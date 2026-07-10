import os
import sys
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])) if getattr(sys, "frozen", False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import config, version, dev_mode, db_version, stopping, BLUE, YELLOW, RED, colored_log, setup_logging, logger
setup_logging()
os.makedirs(os.path.dirname(config["data_dir"]["database"]), exist_ok=True)
from threading import Thread
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import Response, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from app.db import SQLite
from migrations import run_migrations
from app.compat import build_proxy, set_request, reset_request, event_loop_var
from app.responses import to_starlette, send_from_directory, HTTPAbort, redirect, make_response
from app.api_utils import process_cors_headers, cleaner
from app.router import Router
from app.routers.auth import auth_bp
from app.routers.channels import channels_bp
from app.routers.keys import keys_bp
from app.routers.members import members_bp
from app.routers.bans import bans_bp
from app.routers.messages import messages_bp
from app.routers.users import users_bp
from app.routers.pins import pins_bp
from app.routers.stream import stream_bp, start_call_sweep, start_message_expiry_sweep
from app.routers.calls import calls_bp
from app.routers.webhooks import webhooks_bp
from app.routers.legal import legal_bp
from app.routers.push import push_bp
from app.routers.bots import bots_bp
from app.routers.interactions import interactions_bp
from app.routers.oauth import oauth_bp, start_oauth_token_sweep
from app.push import init_vapid
import asyncio

try: run_migrations()
except Exception as e:
    colored_log(RED, "ERROR", f"Migration failed: {e}")
    sys.exit(1)

with SQLite() as db:
    db.create_table("users", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "id": "TEXT UNIQUE NOT NULL", "username": "TEXT UNIQUE NOT NULL", "display_name": "TEXT", "pfp": "TEXT", "passkey": "TEXT NOT NULL", "public_key": "TEXT NOT NULL", "created_at": "INTEGER NOT NULL", "status": "TEXT NOT NULL DEFAULT 'online'", "status_auto": "INTEGER NOT NULL DEFAULT 0", "last_seen": "INTEGER", "share_last_seen": "INTEGER NOT NULL DEFAULT 1", "share_typing": "INTEGER NOT NULL DEFAULT 1", "is_bot": "INTEGER NOT NULL DEFAULT 0", "owner_id": "TEXT", "bot_private": "INTEGER DEFAULT 1", "FOREIGN KEY (pfp)": "REFERENCES files (id) ON DELETE SET NULL", "FOREIGN KEY (owner_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("session", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "user": "TEXT NOT NULL", "token_hash": "TEXT UNIQUE NOT NULL", "id": "TEXT UNIQUE NOT NULL", "device": "TEXT", "browser": "TEXT", "logged_in_at": "INTEGER NOT NULL", "next_challenge": "INTEGER", "FOREIGN KEY (user)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("channels", {"id": "TEXT PRIMARY KEY", "name": "TEXT", "pfp": "TEXT", "type": "INTEGER NOT NULL CHECK (type IN (1, 2, 3))", "permissions": "INTEGER NOT NULL DEFAULT 0", "dm": "TEXT", "invite_code": "TEXT UNIQUE", "created_at": "INTEGER NOT NULL", "default_ttl": "INTEGER", "FOREIGN KEY (pfp)": "REFERENCES files (id) ON DELETE SET NULL"})
    db.add_column("channels", "default_ttl", "INTEGER")
    db.create_table("members", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "user_id": "TEXT", "channel_id": "TEXT", "joined_at": "INTEGER NOT NULL", "permissions": "INTEGER", "message_seq": "INTEGER DEFAULT 0", "interaction_seq": "INTEGER NOT NULL DEFAULT 0", "hidden": "INTEGER CHECK (hidden IS NULL OR hidden = 1)", "UNIQUE": "(user_id, channel_id)", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE"})
    db.add_column("members", "interaction_seq", "INTEGER NOT NULL DEFAULT 0")
    db.create_table("messages", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "id": "TEXT UNIQUE NOT NULL", "channel_id": "TEXT NOT NULL", "user_id": "TEXT NOT NULL", "content": "TEXT NOT NULL", "key": "TEXT", "iv": "TEXT", "timestamp": "INTEGER NOT NULL", "edited_at": "INTEGER", "replied_to": "TEXT", "signature": "TEXT", "signed_timestamp": "INTEGER", "nonce": "TEXT", "webhook_id": "TEXT", "webhook_name": "TEXT", "webhook_pfp": "TEXT", "components": "TEXT", "expires_at": "INTEGER", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.add_column("messages", "components", "TEXT")
    db.add_column("messages", "expires_at", "INTEGER")
    db.create_table("message_pins", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "id": "TEXT UNIQUE NOT NULL", "FOREIGN KEY (id)": "REFERENCES messages (id) ON DELETE CASCADE"})
    db.create_table("files", {"id": "TEXT PRIMARY KEY", "filename": "TEXT", "hash": "TEXT NOT NULL", "size": "INTEGER NOT NULL", "mimetype": "TEXT", "file_type": "TEXT NOT NULL CHECK (file_type IN ('attachment', 'pfp'))", "UNIQUE": "(hash, file_type)"})
    db.create_table("attachment_message", {"file_id": "TEXT NOT NULL", "message_id": "TEXT NOT NULL", "encrypted": "INTEGER NOT NULL DEFAULT 0", "iv": "TEXT", "PRIMARY KEY": "(file_id, message_id)", "FOREIGN KEY (file_id)": "REFERENCES files (id) ON DELETE CASCADE", "FOREIGN KEY (message_id)": "REFERENCES messages (id) ON DELETE CASCADE"})
    db.create_table("channels_keys", {"id": "TEXT NOT NULL", "channel_id": "TEXT", "user_id": "TEXT", "key": "TEXT", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("channels_keys_info", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "key_id": "TEXT UNIQUE NOT NULL", "channel_id": "TEXT", "by": "TEXT", "timestamp": "INTEGER NOT NULL", "expires_at": "INTEGER NOT NULL", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (by)": "REFERENCES users (id) ON DELETE SET NULL"})
    db.create_table("message_reads", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "user_id": "TEXT NOT NULL", "channel_id": "TEXT NOT NULL", "last_message_id": "TEXT NOT NULL", "read_at": "INTEGER NOT NULL", "UNIQUE": "(user_id, channel_id)", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (last_message_id)": "REFERENCES messages (id) ON DELETE CASCADE"})
    db.create_table("bans", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "user_id": "TEXT NOT NULL", "channel_id": "TEXT NOT NULL", "banned_by": "TEXT NOT NULL", "banned_at": "INTEGER NOT NULL", "reason": "TEXT", "UNIQUE": "(user_id, channel_id)", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (banned_by)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("blocks", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "blocker_id": "TEXT NOT NULL", "blocked_id": "TEXT NOT NULL", "blocked_at": "INTEGER NOT NULL", "UNIQUE": "(blocker_id, blocked_id)", "FOREIGN KEY (blocker_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (blocked_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("calls", {"channel_id": "TEXT PRIMARY KEY", "started_by": "TEXT NOT NULL", "started_at": "INTEGER NOT NULL", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (started_by)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("call_participants", {"channel_id": "TEXT NOT NULL", "user_id": "TEXT NOT NULL", "joined_at": "INTEGER NOT NULL", "left_at": "INTEGER", "PRIMARY KEY": "(channel_id, user_id)", "FOREIGN KEY (channel_id)": "REFERENCES calls (channel_id) ON DELETE CASCADE", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("call_history", {"id": "TEXT PRIMARY KEY", "channel_id": "TEXT NOT NULL", "started_by": "TEXT", "started_at": "INTEGER NOT NULL", "ended_at": "INTEGER", "participant_count": "INTEGER NOT NULL DEFAULT 1", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE"})
    db.create_table("webhooks", {"id": "TEXT PRIMARY KEY", "channel_id": "TEXT NOT NULL", "name": "TEXT NOT NULL", "pfp": "TEXT", "token": "TEXT NOT NULL", "created_by": "TEXT", "created_at": "INTEGER NOT NULL", "last_used_at": "INTEGER", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (created_by)": "REFERENCES users (id) ON DELETE SET NULL"})
    db.create_table("push_subscriptions", {"id": "TEXT PRIMARY KEY", "user_id": "TEXT NOT NULL", "endpoint": "TEXT UNIQUE NOT NULL", "p256dh": "TEXT NOT NULL", "auth": "TEXT NOT NULL", "created_at": "INTEGER NOT NULL", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("bot_tokens", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "id": "TEXT UNIQUE NOT NULL", "bot_id": "TEXT NOT NULL", "token_hash": "TEXT UNIQUE NOT NULL", "created_at": "INTEGER NOT NULL", "FOREIGN KEY (bot_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("bot_commands", {"id": "TEXT PRIMARY KEY", "bot_id": "TEXT NOT NULL", "name": "TEXT NOT NULL", "description": "TEXT NOT NULL", "UNIQUE": "(bot_id, name)", "FOREIGN KEY (bot_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("bot_command_options", {"id": "TEXT PRIMARY KEY", "command_id": "TEXT NOT NULL", "name": "TEXT NOT NULL", "description": "TEXT NOT NULL", "type": "TEXT NOT NULL", "required": "INTEGER NOT NULL DEFAULT 0", "min_value": "REAL", "max_value": "REAL", "min_length": "INTEGER", "max_length": "INTEGER", "choices": "TEXT", "position": "INTEGER NOT NULL DEFAULT 0", "FOREIGN KEY (command_id)": "REFERENCES bot_commands (id) ON DELETE CASCADE"})
    db.create_table("interaction_history", {"seq": "INTEGER PRIMARY KEY AUTOINCREMENT", "id": "TEXT UNIQUE NOT NULL", "channel_id": "TEXT NOT NULL", "user_username": "TEXT NOT NULL", "user_display": "TEXT", "command": "TEXT NOT NULL", "bot_username": "TEXT NOT NULL", "timestamp": "INTEGER NOT NULL", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE"})
    db.create_table("component_interactions", {"id": "TEXT PRIMARY KEY", "channel_id": "TEXT NOT NULL", "message_id": "TEXT NOT NULL", "user_id": "TEXT NOT NULL", "bot_id": "TEXT NOT NULL", "custom_id": "TEXT NOT NULL", "timestamp": "INTEGER NOT NULL", "responded": "INTEGER NOT NULL DEFAULT 0", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (message_id)": "REFERENCES messages (id) ON DELETE CASCADE"})
    db.create_table("embed_assets", {"id": "TEXT PRIMARY KEY", "file_id": "TEXT NOT NULL", "channel_id": "TEXT NOT NULL", "uploader_id": "TEXT NOT NULL", "message_id": "TEXT", "key_id": "TEXT", "iv": "TEXT", "encrypted": "INTEGER NOT NULL DEFAULT 0", "created_at": "INTEGER NOT NULL", "FOREIGN KEY (file_id)": "REFERENCES files (id) ON DELETE CASCADE", "FOREIGN KEY (channel_id)": "REFERENCES channels (id) ON DELETE CASCADE", "FOREIGN KEY (uploader_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (message_id)": "REFERENCES messages (id) ON DELETE SET NULL"})
    db.create_table("oauth_apps", {"id": "TEXT PRIMARY KEY", "owner_id": "TEXT NOT NULL", "name": "TEXT NOT NULL", "pfp": "TEXT", "client_secret_hash": "TEXT NOT NULL", "redirect_uris": "TEXT NOT NULL", "created_at": "INTEGER NOT NULL", "FOREIGN KEY (owner_id)": "REFERENCES users (id) ON DELETE CASCADE", "FOREIGN KEY (pfp)": "REFERENCES files (id) ON DELETE SET NULL"})
    db.create_table("oauth_tokens", {"id": "TEXT PRIMARY KEY", "token_hash": "TEXT UNIQUE NOT NULL", "app_id": "TEXT NOT NULL", "user_id": "TEXT NOT NULL", "scope": "TEXT NOT NULL", "created_at": "INTEGER NOT NULL", "expires_at": "INTEGER NOT NULL", "FOREIGN KEY (app_id)": "REFERENCES oauth_apps (id) ON DELETE CASCADE", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_table("oauth_consents", {"app_id": "TEXT NOT NULL", "user_id": "TEXT NOT NULL", "scope": "TEXT NOT NULL", "granted_at": "INTEGER NOT NULL", "PRIMARY KEY": "(app_id, user_id)", "FOREIGN KEY (app_id)": "REFERENCES oauth_apps (id) ON DELETE CASCADE", "FOREIGN KEY (user_id)": "REFERENCES users (id) ON DELETE CASCADE"})
    db.create_index("component_interactions", ["bot_id", "responded"])
    db.create_index("embed_assets", "file_id")
    db.create_index("embed_assets", "message_id")
    db.create_index("embed_assets", "created_at")
    db.create_index("session", "user")
    db.create_index("members", "channel_id")
    db.create_index("members", "message_seq")
    db.create_index("messages", "channel_id")
    db.create_index("messages", "user_id")
    db.create_index("messages", "timestamp")
    db.create_index("messages", "expires_at")
    db.create_index("files", "file_type")
    db.create_index("attachment_message", "message_id")
    db.create_index("channels_keys", "id")
    db.create_index("channels_keys", "channel_id")
    db.create_index("channels_keys", "user_id")
    db.create_index("channels_keys_info", "channel_id")
    db.create_index("message_reads", "user_id")
    db.create_index("message_reads", "channel_id")
    db.create_index("call_participants", "channel_id")
    db.create_index("call_participants", "user_id")
    db.create_index("call_history", "channel_id")
    db.create_index("webhooks", "channel_id")
    db.create_index("webhooks", "token", unique=True)
    db.create_index("push_subscriptions", "user_id")
    db.create_index("bot_tokens", "bot_id")
    db.create_index("bot_commands", "bot_id")
    db.create_index("bot_command_options", "command_id")
    db.create_index("oauth_apps", "owner_id")
    db.create_index("oauth_tokens", "expires_at")
    db.create_index("oauth_tokens", ["app_id", "user_id"])
    db.create_index("interaction_history", ["channel_id", "timestamp"])
    db.create_index("interaction_history", ["channel_id", "seq"])
    db.create_index("users", "owner_id")
    if not db.exists("users", {"id": "0"}): db.insert_data("users", {"id": "0", "username": "__holt_webhooks_system_account_do_not_use__", "display_name": "System", "pfp": None, "passkey": "system", "public_key": "system", "created_at": 0})
    if config["presence"]["enabled"]: db.execute_raw_sql("UPDATE users SET last_seen=(SELECT MAX(timestamp) FROM messages WHERE user_id=users.id) WHERE last_seen IS NULL AND EXISTS(SELECT 1 FROM messages WHERE user_id=users.id)")
    if db.execute_raw_sql("PRAGMA user_version;")[0]["user_version"]!=db_version: db.execute_raw_sql(f"PRAGMA user_version={db_version};")
    db.execute_raw_sql("DELETE FROM call_participants")
    db.execute_raw_sql("DELETE FROM calls")

init_vapid()
uri_prefix="/"+config["uri_prefix"] if config["uri_prefix"] else ""
def route_rule(rule: str): return uri_prefix+rule

frontend_hosted=config["frontend"]["hosted"]
frontend_present=os.path.isdir(config["frontend"]["frontend_directory"])
frontend=frontend_hosted and frontend_present

error_text={
    "404": "not found",
    "405": "method not allowed",
    "400": "bad request",
    "413": "content too big",
    "415": "unsupported media type",
    "500": "internal server error"
}

api_url=route_rule("/api/v1/")

def _flask_to_starlette_rule(rule):
    out=""
    i=0
    while i<len(rule):
        if rule[i]=="<":
            j=rule.index(">", i)
            inner=rule[i+1:j]
            if ":" in inner:
                converter, var=inner.split(":", 1)
            else:
                converter, var="default", inner
            out+="{"+var+(":path" if converter=="path" else "")+"}"
            i=j+1
        else:
            out+=rule[i]
            i+=1
    return out

def make_endpoint(method_map, is_api, allow_methods, explicit_options):
    allow_header=", ".join(allow_methods)
    async def endpoint(req):
        method=req.method
        if method=="OPTIONS" and not explicit_options:
            resp=Response(status_code=200, headers={"Allow": allow_header})
            if is_api: process_cors_headers(resp)
            return resp
        handler=method_map.get(method) or (method_map.get("GET") if method=="HEAD" else None)
        loop=asyncio.get_running_loop()
        proxy=await build_proxy(req)
        token=set_request(proxy)
        loop_token=event_loop_var.set(loop)
        try:
            path_params=req.path_params
            rv=await run_in_threadpool(handler, **path_params)
            resp=to_starlette(rv)
        except HTTPAbort as e:
            resp=_error_response(e.code, req)
        finally:
            event_loop_var.reset(loop_token)
            reset_request(token)
        if is_api: process_cors_headers(resp)
        return resp
    return endpoint

def _error_response(code, req):
    path=req.scope.get("root_path", "")+req.scope.get("path", "")
    if path.startswith(uri_prefix):
        if path==api_url or (path+"/").startswith(api_url):
            from app.responses import jsonify
            resp=to_starlette((jsonify({"error": error_text[str(code)], "success": False}), code))
            process_cors_headers(resp)
            return resp
        if frontend:
            try: return to_starlette((send_from_directory(config["frontend"]["frontend_directory"], f"{code}.html"), code))
            except HTTPAbort: return to_starlette((error_text[str(code)], code))
        return to_starlette((error_text[str(code)], code))
    return to_starlette(({"error": error_text[str(code)]}, code))

routes=[]
_path_groups={}
_path_order=[]

def register_router(router, prefix):
    for rule, methods, func in router.routes:
        full=route_rule(prefix+rule)
        path=_flask_to_starlette_rule(full)
        if path not in _path_groups:
            _path_groups[path]={}
            _path_order.append(path)
        for m in methods:
            _path_groups[path][m]=func

API_PREFIX="/api/v1"
for r in [auth_bp, channels_bp, keys_bp, members_bp, bans_bp, messages_bp, users_bp, pins_bp, stream_bp, calls_bp, webhooks_bp, legal_bp, push_bp, bots_bp, interactions_bp, oauth_bp]:
    register_router(r, API_PREFIX)

for path in _path_order:
    method_map=_path_groups[path]
    explicit_options="OPTIONS" in method_map
    all_methods=set(method_map.keys())
    if "GET" in all_methods: all_methods.add("HEAD")
    all_methods.add("OPTIONS")
    allow=[m for m in ["HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH", "DELETE"] if m in all_methods]
    routes.append(Route(path, make_endpoint(method_map, True, allow, explicit_options), methods=sorted(all_methods)))

async def api_index(req):
    resp=to_starlette(redirect(api_url, 301))
    process_cors_headers(resp)
    return resp
routes.append(Route(route_rule("/api/v1"), api_index, methods=["GET"]))

def serve_pfp_handler(pfp):
    db=SQLite()
    try:
        pfp_data=db.select_data("files", ["id", "mimetype"], {"id": pfp, "file_type": "pfp"})
        if not pfp_data: raise HTTPAbort(404)
        try:
            resp=send_from_directory(config["data_dir"]["pfps"], f"{pfp_data[0]['id']}.webp", mimetype=pfp_data[0]["mimetype"], as_attachment=False)
            process_cors_headers(resp)
            return resp
        except HTTPAbort:
            db.cleanup_unused_files()
            raise HTTPAbort(404)
    finally: db.close()

def serve_attachment_handler(file_id):
    db=SQLite()
    try:
        file_data=db.select_data("files", ["id", "filename", "mimetype"], {"id": file_id, "file_type": "attachment"})
        if not file_data: raise HTTPAbort(404)
        filename=file_data[0]["filename"] or "attachment"
        try:
            resp=send_from_directory(config["data_dir"]["attachments"], file_data[0]["id"], mimetype=file_data[0]["mimetype"], as_attachment=True, download_name=filename)
            process_cors_headers(resp)
            return resp
        except HTTPAbort:
            db.cleanup_unused_files()
            raise HTTPAbort(404)
    finally: db.close()

async def serve_pfp(req):
    try: return to_starlette(await run_in_threadpool(serve_pfp_handler, req.path_params["pfp"]))
    except HTTPAbort as e: return _error_response(e.code, req)

async def serve_attachment(req):
    try: return to_starlette(await run_in_threadpool(serve_attachment_handler, req.path_params["file_id"]))
    except HTTPAbort as e: return _error_response(e.code, req)

routes.append(Route(route_rule("/pfp/{pfp}"), serve_pfp, methods=["GET"]))
routes.append(Route(route_rule("/attachment/{file_id}"), serve_attachment, methods=["GET"]))

async def health(req): return to_starlette({"status": "ok"})
routes.append(Route(route_rule("/health"), health, methods=["GET"]))

if frontend:
    colored_log(BLUE, "INFO", "Frontend directory present, serving it")
    excluded=[i.lower() for i in config["frontend"]["excluded_frontend_root_paths"]]

    def safe_join(directory, *pathnames):
        parts=[directory]
        for filename in pathnames:
            if filename!="":
                filename=os.path.normpath(filename)
            if (os.path.isabs(filename) or filename==".." or filename.startswith("../") or os.sep+".."+os.sep in os.sep+filename+os.sep):
                return None
            parts.append(filename)
        return os.path.join(*parts)

    def index_handler():
        fdir=config["frontend"]["frontend_directory"]
        path=os.path.join(fdir, "index.html")
        if not os.path.isfile(path): raise HTTPAbort(404)
        with open(path, encoding="utf-8") as f: html=f.read()
        return html

    def serve_static_handler(path):
        if "." not in path: path+=".html"
        safe_path=safe_join(config["frontend"]["frontend_directory"], path)
        if not safe_path: raise HTTPAbort(404)
        safe_path=os.path.relpath(safe_path, config["frontend"]["frontend_directory"]).lower()
        for exclude in excluded:
            if safe_path.startswith(exclude+os.sep) or safe_path==exclude: raise HTTPAbort(404)
        return send_from_directory(config["frontend"]["frontend_directory"], path)

    async def index(req):
        try: resp=to_starlette(await run_in_threadpool(index_handler)); resp.headers["Cache-Control"]="no-cache"; return resp
        except HTTPAbort as e: return _error_response(e.code, req)
    async def serve_static(req):
        try: return to_starlette(await run_in_threadpool(serve_static_handler, req.path_params["path"]))
        except HTTPAbort as e: return _error_response(e.code, req)
    routes.append(Route(route_rule("/"), index, methods=["GET"]))
    routes.append(Route(route_rule("/{path:path}"), serve_static, methods=["GET"]))
elif not frontend_hosted:
    colored_log(BLUE, "INFO", "Frontend directory isn't hosted")
elif not frontend_present:
    colored_log(RED, "ERROR", "Frontend directory isn't present")

async def starlette_http_exc(req, exc):
    code=exc.status_code
    if str(code) in error_text: return _error_response(code, req)
    return PlainTextResponse(getattr(exc, "detail", "") or "", status_code=code)

async def server_error(req, exc):
    logger.exception("Unhandled error")
    return _error_response(500, req)

exception_handlers={StarletteHTTPException: starlette_http_exc, Exception: server_error}

HTTP_STATUS_CODES={100: "Continue", 101: "Switching Protocols", 102: "Processing", 103: "Early Hints", 200: "OK", 201: "Created", 202: "Accepted", 203: "Non Authoritative Information", 204: "No Content", 205: "Reset Content", 206: "Partial Content", 207: "Multi Status", 208: "Already Reported", 226: "IM Used", 300: "Multiple Choices", 301: "Moved Permanently", 302: "Found", 303: "See Other", 304: "Not Modified", 305: "Use Proxy", 306: "Switch Proxy", 307: "Temporary Redirect", 308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 406: "Not Acceptable", 407: "Proxy Authentication Required", 408: "Request Timeout", 409: "Conflict", 410: "Gone", 411: "Length Required", 412: "Precondition Failed", 413: "Request Entity Too Large", 414: "Request URI Too Long", 415: "Unsupported Media Type", 416: "Requested Range Not Satisfiable", 417: "Expectation Failed", 418: "I'm a teapot", 421: "Misdirected Request", 422: "Unprocessable Entity", 423: "Locked", 424: "Failed Dependency", 425: "Too Early", 426: "Upgrade Required", 428: "Precondition Required", 429: "Too Many Requests", 431: "Request Header Fields Too Large", 449: "Retry With", 451: "Unavailable For Legal Reasons", 500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout", 505: "HTTP Version Not Supported", 506: "Variant Also Negotiates", 507: "Insufficient Storage", 508: "Loop Detected", 510: "Not Extended", 511: "Network Authentication Failed"}

def _patch_uvicorn_reason_phrases():
    """Match waitress/werkzeug reason phrases (uppercased) for byte-identical status lines."""
    def phrase(code): return (HTTP_STATUS_CODES.get(code, "UNKNOWN")).upper().encode()
    try:
        import uvicorn.protocols.http.httptools_impl as ht
        ht.STATUS_LINE={code: b"".join([b"HTTP/1.1 ", str(code).encode(), b" ", phrase(code), b"\r\n"]) for code in range(100, 600)}
    except Exception: pass
    try:
        import uvicorn.protocols.http.h11_impl as h11i
        h11i.STATUS_PHRASES={code: phrase(code) for code in range(100, 600)}
    except Exception: pass

_patch_uvicorn_reason_phrases()

app=Starlette(routes=routes, exception_handlers=exception_handlers)

Thread(target=cleaner, daemon=True).start()
start_call_sweep()
start_message_expiry_sweep()
start_oauth_token_sweep()

colored_log(BLUE, "INFO", f"Access instance at http://{config['server']['host']}:{config['server']['port']}{uri_prefix}/")
if dev_mode: colored_log(YELLOW, "WARNING", "Dev mode is enabled, please disable this mode if you're running this in production")
if dev_mode: colored_log(BLUE, "DEV MODE INFO", f"Access instance at http://localhost:{config['server']['port']}{uri_prefix}/ for local access")

def run():
    import uvicorn
    proxy_headers=config["server"]["proxy"]
    try:
        uvicorn.run(app, host=config["server"]["host"], port=config["server"]["port"], log_level="debug" if dev_mode else "info", proxy_headers=proxy_headers, forwarded_allow_ips="*" if proxy_headers else None)
    except KeyboardInterrupt: pass
    finally:
        colored_log(BLUE, "LOG", "Exiting...")
        stopping.set()
