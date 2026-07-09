import json
import hashlib
import base64
from threading import Thread
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import (
    make_json_error, logged_in, pass_db, sliding_window_rate_limiter, validate_request_data,
    timestamp, hash_token, handle_pfp, oauth_codes, oauth_codes_lock
)
from app.config import generate, config
from app.db import SQLite

oauth_bp=Router("oauth")
SCOPES={"identify"}

def _validate_redirect_uris(uris):
    if not isinstance(uris, list) or not uris: return "redirect_uris must be a non-empty array"
    if len(uris)>10: return "Maximum 10 redirect URIs"
    for uri in uris:
        if not isinstance(uri, str) or not (uri.startswith("https://") or uri.startswith("http://")) or len(uri)>500: return f"Invalid redirect_uri: {uri!r}"
    return None

def _app_summary(db, app_id):
    app=db.select_data("oauth_apps", ["id", "name", "pfp", "redirect_uris", "created_at"], {"id": app_id})
    if not app: return None
    row=app[0]
    row["redirect_uris"]=json.loads(row["redirect_uris"])
    return row

def _append_query(uri, params):
    parts=urlsplit(uri)
    query=parse_qsl(parts.query, keep_blank_values=True)
    query.extend(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

@oauth_bp.route("/oauth/apps")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def list_apps(db:SQLite, id):
    apps=db.select_data("oauth_apps", ["id", "name", "pfp", "redirect_uris", "created_at"], {"owner_id": id}, "created_at DESC")
    for app in apps: app["redirect_uris"]=json.loads(app["redirect_uris"])
    return jsonify(apps)

@oauth_bp.route("/oauth/apps", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=10, window=300, user_limit=5)
@validate_request_data({"name": {"minlen": 2, "maxlen": 40}, "redirect_uris": {}})
def create_app(db:SQLite, id):
    oauth_config=config.get("oauth", {})
    if not oauth_config.get("enabled", True): return make_json_error(403, "OAuth apps are disabled")
    max_apps=oauth_config.get("max_apps_per_user", 5)
    app_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM oauth_apps WHERE owner_id=?", (id,))[0]["count"]
    if app_count>=max_apps: return make_json_error(403, "You have reached the maximum number of OAuth apps")
    try: redirect_uris=json.loads(request.form["redirect_uris"])
    except Exception: return make_json_error(400, "Invalid redirect_uris format")
    err=_validate_redirect_uris(redirect_uris)
    if err: return make_json_error(400, err)
    pfp_result=handle_pfp(db=db)
    if isinstance(pfp_result, tuple): return pfp_result
    app_id=generate()
    client_secret=generate(40)
    now=timestamp()
    db.insert_data("oauth_apps", {"id": app_id, "owner_id": id, "name": request.form["name"], "pfp": pfp_result, "client_secret_hash": hash_token(client_secret), "redirect_uris": json.dumps(redirect_uris), "created_at": now})
    return jsonify({"app": _app_summary(db, app_id), "client_id": app_id, "client_secret": client_secret, "success": True}), 201

@oauth_bp.route("/oauth/apps/<string:app_id>", methods=["PATCH"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def edit_app(db:SQLite, id, app_id):
    if not db.exists("oauth_apps", {"id": app_id, "owner_id": id}): return make_json_error(404, "App not found")
    update_data={}
    errors=[]
    if "name" in request.form:
        if len(request.form["name"])>1 and len(request.form["name"])<41: update_data["name"]=request.form["name"]
        else: errors.append("Invalid name parameter, error: length")
    if "redirect_uris" in request.form:
        try: redirect_uris=json.loads(request.form["redirect_uris"])
        except Exception: return make_json_error(400, "Invalid redirect_uris format")
        err=_validate_redirect_uris(redirect_uris)
        if err: errors.append(err)
        else: update_data["redirect_uris"]=json.dumps(redirect_uris)
    if request.files and "pfp" in request.files:
        pfp_result=handle_pfp(error_as_text=True, db=db)
        if not isinstance(pfp_result, tuple):
            if pfp_result:
                old_pfp_data=db.execute_raw_sql("SELECT pfp FROM oauth_apps WHERE id=?", (app_id,))
                old_pfp_id=old_pfp_data[0]["pfp"] if old_pfp_data and old_pfp_data[0]["pfp"] else None
                if old_pfp_id!=pfp_result:
                    update_data["pfp"]=pfp_result
                    if old_pfp_id: db.cleanup_unused_files()
                else: errors.append("Icon is the same")
        else: errors.append(pfp_result[0])
    if not update_data: return jsonify({"error": "No valid parameters to update", "errors": errors, "success": False}), 400
    db.update_data("oauth_apps", update_data, {"id": app_id})
    return jsonify({"app": _app_summary(db, app_id), "errors": errors, "success": True})

@oauth_bp.route("/oauth/apps/<string:app_id>/secret", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=10, window=300, user_limit=5)
def regenerate_secret(db:SQLite, id, app_id):
    if not db.exists("oauth_apps", {"id": app_id, "owner_id": id}): return make_json_error(404, "App not found")
    client_secret=generate(40)
    db.update_data("oauth_apps", {"client_secret_hash": hash_token(client_secret)}, {"id": app_id})
    return jsonify({"client_secret": client_secret, "success": True}), 201

@oauth_bp.route("/oauth/apps/<string:app_id>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def delete_app(db:SQLite, id, app_id):
    app=db.select_data("oauth_apps", ["pfp"], {"id": app_id, "owner_id": id})
    if not app: return make_json_error(404, "App not found")
    db.delete_data("oauth_apps", {"id": app_id})
    if app[0]["pfp"]: db.cleanup_unused_files()
    return jsonify({"success": True})

@oauth_bp.route("/oauth/authorize/info")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def authorize_info(db:SQLite, id):
    client_id=request.args.get("client_id")
    redirect_uri=request.args.get("redirect_uri")
    scope=request.args.get("scope") or "identify"
    if not client_id or not redirect_uri: return make_json_error(400, "client_id and redirect_uri are required")
    app=db.select_data("oauth_apps", ["id", "name", "pfp", "redirect_uris"], {"id": client_id})
    if not app: return make_json_error(404, "Application not found")
    app=app[0]
    if redirect_uri not in json.loads(app["redirect_uris"]): return make_json_error(400, "Invalid redirect_uri")
    if not set(scope.split())<=SCOPES: return make_json_error(400, "Invalid scope")
    already_consented=db.exists("oauth_consents", {"app_id": client_id, "user_id": id, "scope": scope})
    return jsonify({"app": {"id": app["id"], "name": app["name"], "pfp": app["pfp"]}, "scope": scope, "already_consented": already_consented, "success": True})

@oauth_bp.route("/oauth/authorize/decision", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
@validate_request_data({"client_id": {}, "redirect_uri": {}, "allow": {}})
def authorize_decision(db:SQLite, id):
    client_id=request.form["client_id"]
    redirect_uri=request.form["redirect_uri"]
    scope=request.form.get("scope") or "identify"
    state=request.form.get("state", "")
    allow=request.form["allow"] in ("1", "true", "True")
    app=db.select_data("oauth_apps", ["id", "redirect_uris"], {"id": client_id})
    if not app: return make_json_error(404, "Application not found")
    if redirect_uri not in json.loads(app[0]["redirect_uris"]): return make_json_error(400, "Invalid redirect_uri")
    if not set(scope.split())<=SCOPES: return make_json_error(400, "Invalid scope")
    if not allow:
        return jsonify({"redirect_to": _append_query(redirect_uri, {"error": "access_denied", "state": state}), "success": True})
    code_challenge=request.form.get("code_challenge", "")
    code_challenge_method=request.form.get("code_challenge_method", "S256")
    if not (43<=len(code_challenge)<=128) or code_challenge_method!="S256": return make_json_error(400, "PKCE code_challenge (S256) is required")
    now=timestamp()
    if db.exists("oauth_consents", {"app_id": client_id, "user_id": id}):
        db.update_data("oauth_consents", {"scope": scope, "granted_at": now}, {"app_id": client_id, "user_id": id})
    else:
        db.insert_data("oauth_consents", {"app_id": client_id, "user_id": id, "scope": scope, "granted_at": now})
    code=generate(40)
    with oauth_codes_lock:
        oauth_codes[code]={"user_id": id, "app_id": client_id, "redirect_uri": redirect_uri, "scope": scope, "code_challenge": code_challenge, "expire": now+120}
    return jsonify({"redirect_to": _append_query(redirect_uri, {"code": code, "state": state}), "success": True})

@oauth_bp.route("/oauth/token", methods=["POST"])
@sliding_window_rate_limiter(limit=30, window=60)
@pass_db
@validate_request_data({"grant_type": {}, "code": {}, "redirect_uri": {}, "client_id": {}, "client_secret": {}, "code_verifier": {}})
def token(db:SQLite):
    if request.form["grant_type"]!="authorization_code": return make_json_error(400, "Unsupported grant_type")
    client_id=request.form["client_id"]
    if not db.exists("oauth_apps", {"id": client_id, "client_secret_hash": hash_token(request.form["client_secret"])}): return make_json_error(401, "Invalid client credentials")
    code=request.form["code"]
    with oauth_codes_lock: code_data=oauth_codes.pop(code, None)
    if not code_data or code_data["expire"]<timestamp(): return make_json_error(400, "Invalid or expired code")
    if code_data["app_id"]!=client_id or code_data["redirect_uri"]!=request.form["redirect_uri"]: return make_json_error(400, "Invalid code for this client/redirect_uri")
    computed_challenge=base64.urlsafe_b64encode(hashlib.sha256(request.form["code_verifier"].encode()).digest()).decode().rstrip("=")
    if computed_challenge!=code_data["code_challenge"]: return make_json_error(400, "Invalid code_verifier")
    access_token=generate(40)
    now=timestamp(True)
    db.insert_data("oauth_tokens", {"id": generate(), "token_hash": hash_token(access_token), "app_id": client_id, "user_id": code_data["user_id"], "scope": code_data["scope"], "created_at": now, "expires_at": now+3600000})
    return jsonify({"access_token": access_token, "token_type": "Bearer", "expires_in": 3600, "scope": code_data["scope"]})

@oauth_bp.route("/oauth/userinfo")
@sliding_window_rate_limiter(limit=120, window=60)
@pass_db
def userinfo(db:SQLite):
    if "authorization" not in request.headers: return make_json_error(401, "Authorization header missing")
    auth_split=request.headers["authorization"].split(" ")
    if len(auth_split)<2 or auth_split[0]!="Bearer": return make_json_error(401, "Bad authorization header")
    token_data=db.select_data("oauth_tokens", ["user_id", "scope", "expires_at"], {"token_hash": hash_token(auth_split[1])})
    if not token_data: return make_json_error(401, "Invalid access token")
    token_data=token_data[0]
    if token_data["expires_at"]<timestamp(True): return make_json_error(401, "Access token expired")
    user=db.select_data("users", ["id", "username", "display_name", "pfp"], {"id": token_data["user_id"]})
    if not user: return make_json_error(404, "User not found")
    return jsonify({**user[0], "scope": token_data["scope"], "success": True})

@oauth_bp.route("/oauth/revoke", methods=["POST"])
@sliding_window_rate_limiter(limit=30, window=60)
@pass_db
@validate_request_data({"token": {}})
def revoke(db:SQLite):
    db.delete_data("oauth_tokens", {"token_hash": hash_token(request.form["token"])})
    return jsonify({"success": True})

def _oauth_token_sweep():
    """Periodically delete expired OAuth access tokens; not correctness-critical since expiry is checked at use-time"""
    from app.config import stopping
    while not stopping.is_set():
        db=SQLite()
        try: db.execute_raw_sql("DELETE FROM oauth_tokens WHERE expires_at<?", (timestamp(True),))
        finally: db.close()
        stopping.wait(600)

def start_oauth_token_sweep():
    Thread(target=_oauth_token_sweep, daemon=True).start()
