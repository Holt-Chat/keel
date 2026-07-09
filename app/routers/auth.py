from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.config import version, dev_mode
from app.api_utils import (
    make_json_error, logged_in, pass_db, validate_request_data, sliding_window_rate_limiter,
    public_key_open, get_challenge, timestamp, challenges, challenges_lock,
    regex_first_group_encrypted, browser_regex, device_regex, rsa_encrypt,
    get_channel_last_message_seq, hash_token, device_links, device_links_lock
)
from app.config import generate
from app.db import SQLite
import bcrypt
import re
from app.config import config, RED, colored_log
from app.routers.stream import channel_added, member_join
from app.routers.legal import legal_present
from app.push import get_public_key

auth_bp=Router("auth")

@auth_bp.route("/")
def index(): return {"running": "Holt", "version": version,
  "max_file_size": config["max_file_size"], "messages": config["messages"],
  "disable_channel_creation": config["instance"]["disable_channel_creation"],
  "disable_channel_deletion": config["instance"]["disable_channel_deletion"],
  "max_channels": config["max_members"]["max_channels"], "password_protected": bool(config["instance"]["password"]),
  "calls": config["calls"], "webhooks": config["webhooks"],
  "presence": config["presence"], "typing": config["typing"],
  "push": {"enabled": config["push"]["enabled"], "vapid_public_key": get_public_key()},
  "legal": legal_present(),
  **({"dev": True} if dev_mode else {})}, 200

def join_invite(db, id, invite_code):
    invite_data=db.execute_raw_sql("""
        SELECT c.id, c.type,
               EXISTS(SELECT 1 FROM members WHERE user_id=? AND channel_id=c.id) as is_member,
               EXISTS(SELECT 1 FROM bans WHERE user_id=? AND channel_id=c.id) as is_banned,
               (SELECT COUNT(*) FROM members WHERE user_id=? AND hidden IS NULL) as user_channel_count,
               (SELECT COUNT(*) FROM members m JOIN users u ON u.id=m.user_id WHERE m.channel_id=c.id AND u.is_bot=0) as channel_member_count
        FROM channels c
        WHERE c.invite_code=?
    """, (id, id, id, invite_code))
    if not invite_data: return "Instance invite not found"
    data=invite_data[0]
    channel_id=data["id"]
    if data["is_member"]: return "User is already a member of instance invite channel"
    if data["is_banned"]: return "User is banned from instance invite channel"
    if data["user_channel_count"]>=config["max_members"]["max_channels"]: return "User has reached the maximum number of channels"
    channel_type=data["type"]
    if channel_type!=3:
        if data["channel_member_count"]>=config["max_members"]["encrypted_channels"]: return "Instance invite channel has reached maximum member limit"
    db.insert_data("members", {"user_id": id, "channel_id": channel_id, "joined_at": timestamp(), "message_seq": 0 if channel_type==3 else get_channel_last_message_seq(db, channel_id)})

    # Get user and channel data and emit events
    user_channel_data=db.execute_raw_sql("""
        SELECT u.id, u.username, u.display_name, u.pfp,
               c.name, c.pfp as channel_pfp, c.type, c.permissions,
               COUNT(m.user_id) as member_count
        FROM users u, channels c
        LEFT JOIN members m ON c.id=m.channel_id
        WHERE u.id=? AND c.id=?
        GROUP BY c.id
    """, (id, channel_id))[0]
    user_data={"id": user_channel_data["id"], "username": user_channel_data["username"], "display_name": user_channel_data["display_name"], "pfp": user_channel_data["pfp"]}
    full_channel_data={"id": channel_id, "name": user_channel_data["name"], "pfp": user_channel_data["channel_pfp"], "type": user_channel_data["type"], "permissions": user_channel_data["permissions"], "member_count": user_channel_data["member_count"]}
    member_join(channel_id, user_data, db)
    channel_added(id, full_channel_data, db)

@auth_bp.route("/solve", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
@validate_request_data({"id": {"len": 20}, "solve": {"len": 20}})
def solve():
    with challenges_lock:
        if request.form["id"] not in challenges: return make_json_error(400, "Invalid challenge ID")
        if challenges[request.form["id"]]["expire"]<timestamp():
            del challenges[request.form["id"]]
            return make_json_error(400, "Invalid challenge ID")
        hashed_challenge=challenges[request.form["id"]]["hashed"]
        logged_in_at=challenges[request.form["id"]].get("logged_in_at")
        new="new" in challenges[request.form["id"]]
        reset_passkey="reset_passkey" in challenges[request.form["id"]]
        if new:
            public_key_text=challenges[request.form["id"]]["public"]
            username=challenges[request.form["id"]]["username"]
        elif reset_passkey:
            user_id=challenges[request.form["id"]]["user_id"]
        else: id=challenges[request.form["id"]]["id"]
        del challenges[request.form["id"]]
    if not bcrypt.checkpw(request.form["solve"].encode(), hashed_challenge.encode()): return make_json_error(401, "Challenge failed")
    db=SQLite()
    if new:
        public_key, error_resp=public_key_open(public_key_text)
        if error_resp: return error_resp
        id=generate()
        passkey=generate()
        hashed_passkey=bcrypt.hashpw(passkey.encode(), bcrypt.gensalt()).decode()
        try:
            db.insert_data("users", {"id": id, "username": username, "passkey": hashed_passkey, "public_key": public_key_text, "created_at": timestamp()})
        except Exception as e:
            if "UNIQUE constraint failed" in str(e): return make_json_error(400, "Username is in use")
            raise
        if config["instance"]["invite"]:
            invite_error=join_invite(db, id, config["instance"]["invite"])
            if invite_error: colored_log(RED, "ERROR", invite_error)
    elif reset_passkey:
        new_passkey=generate()
        hashed_passkey=bcrypt.hashpw(new_passkey.encode(), bcrypt.gensalt()).decode()
        db.update_data("users", {"passkey": hashed_passkey}, {"id": user_id})
        user_public=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (user_id,))[0]["public_key"]
        public_key, error_resp=public_key_open(user_public)
        if error_resp: return error_resp
        id=user_id
    else:
        public_key_data=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (id,))
        if not public_key_data: return make_json_error(400, "User not found")
        public_key, error_resp=public_key_open(public_key_data[0]["public_key"])
        if error_resp: return error_resp
    if not reset_passkey:
        if "User-Agent" in request.headers:
            browser=regex_first_group_encrypted(browser_regex.search(request.headers["User-Agent"]), public_key)
            device=regex_first_group_encrypted(device_regex.search(request.headers["User-Agent"]), public_key)
        else: browser=device=None
        session=generate(50)
        db.insert_data("session", {"user": id, "token_hash": hash_token(session), "id": generate(), "browser": browser, "device": device, "logged_in_at": logged_in_at or timestamp(), "next_challenge": timestamp()+3600})
    db.close()
    if reset_passkey: return jsonify({"passkey": new_passkey, "success": True})
    return jsonify({"session": session, "success": True, **(({"passkey": passkey} if new else {}))})

@auth_bp.route("/username_check")
@sliding_window_rate_limiter(limit=50, window=60)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}}, source="args")
@pass_db
def username_check(db:SQLite): return make_json_error(400, "Username is in use") if db.exists("users", {"username": request.args["username"]}) else jsonify({"success": True})

@auth_bp.route("/signup", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=120)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": re.compile(r"[a-z0-9_\-]+")}, "public": {"len": 392}}, 401)
@pass_db
def signup(db:SQLite):
    if config["instance"]["password"]:
        if "password" not in request.form: return make_json_error(403, "Password required")
        if request.form["password"]!=config["instance"]["password"]: return make_json_error(403, "Password incorrect")
    if db.exists("users", {"username": request.form["username"]}): return make_json_error(400, "Username is in use")
    db.close()
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"new": True, "username": request.form["username"], "hashed": challenge_hash, "expire": timestamp()+60, "public": request.form["public"]}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/login", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20}, "passkey": {"len": 20}, "public": {"len": 392}}, 401)
@pass_db
def login(db:SQLite):
    user=db.select_data("users", ["id", "passkey", "public_key"], {"username": request.form["username"]})
    db.close()
    if not user: return make_json_error(401, "Invalid login details")
    if not bcrypt.checkpw(request.form["passkey"].encode(), user[0]["passkey"].encode()): return make_json_error(401, "Invalid login details")
    if request.form["public"]!=user[0]["public_key"]: return make_json_error(401, "Public key doesn't match")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    id=generate()
    with challenges_lock: challenges[id]={"id": user[0]["id"], "hashed": challenge_hash, "expire": timestamp()+60}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/reset-passkey", methods=["POST"])
@sliding_window_rate_limiter(limit=10, window=600, user_limit=5)
@validate_request_data({"public": {"len": 392}}, 401)
@logged_in()
def reset_passkey(db:SQLite, id):
    user_public_data=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (id,))
    if not user_public_data: return make_json_error(400, "User not found")
    if request.form["public"]!=user_public_data[0]["public_key"]: return make_json_error(401, "Public key doesn't match")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    id, challenge_hash, challenge_enc=get_challenge(public_key)
    with challenges_lock: challenges[id]={"reset_passkey": True, "user_id": id, "hashed": challenge_hash, "expire": timestamp()+60}
    return jsonify({"id": id, "challenge": challenge_enc, "success": True})

@auth_bp.route("/devicelink/start", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120, user_limit=10)
@logged_in()
def devicelink_start(id):
    code=generate(20)
    with device_links_lock: device_links[code]={"user_id": id, "expire": timestamp()+300, "b_public": None, "b_browser": None, "b_device": None, "status": "pending", "session_enc": None, "blob": None}
    return jsonify({"code": code})

@auth_bp.route("/devicelink/claim", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120)
@validate_request_data({"code": {"len": 20}, "public": {"len": 392}})
def devicelink_claim():
    code=request.form["code"]
    with device_links_lock:
        if code not in device_links: return make_json_error(400, "Invalid code")
        link=device_links[code]
        if link["expire"]<timestamp(): return make_json_error(400, "Invalid code")
        if link["status"]!="pending": return make_json_error(400, "Code already claimed")
        link["b_public"]=request.form["public"]
        if "User-Agent" in request.headers:
            browser_match=browser_regex.search(request.headers["User-Agent"])
            device_match=device_regex.search(request.headers["User-Agent"])
            link["b_browser"]=browser_match.group(1)[:50] if browser_match else None
            link["b_device"]=device_match.group(1)[:50] if device_match else None
        link["status"]="claimed"
    return jsonify({"success": True})

@auth_bp.route("/devicelink/status")
@sliding_window_rate_limiter(limit=120, window=60, user_limit=120)
@validate_request_data({"code": {"len": 20}}, source="args")
@logged_in()
def devicelink_status(id):
    code=request.args["code"]
    with device_links_lock:
        if code not in device_links or device_links[code]["expire"]<timestamp(): return jsonify({"status": "expired"})
        link=device_links[code]
        if link["user_id"]!=id: return make_json_error(403, "Not your device link")
        return jsonify({"status": link["status"], "public": link["b_public"], "browser": link["b_browser"], "device": link["b_device"]})

@auth_bp.route("/devicelink/approve", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120, user_limit=10)
@validate_request_data({"code": {"len": 20}, "blob": {}})
@logged_in()
def devicelink_approve(db:SQLite, id):
    code=request.form["code"]
    with device_links_lock:
        if code not in device_links or device_links[code]["expire"]<timestamp(): return make_json_error(400, "Invalid code")
        link=device_links[code]
        if link["user_id"]!=id: return make_json_error(403, "Not your device link")
        if link["status"]!="claimed": return make_json_error(400, "Code is not ready to approve")
        account_public_data=db.execute_raw_sql("SELECT public_key FROM users WHERE id=?", (link["user_id"],))
        if not account_public_data: return make_json_error(400, "User not found")
        account_public, error_resp=public_key_open(account_public_data[0]["public_key"])
        if error_resp: return error_resp
        if "User-Agent" in request.headers:
            browser=regex_first_group_encrypted(browser_regex.search(request.headers["User-Agent"]), account_public)
            device=regex_first_group_encrypted(device_regex.search(request.headers["User-Agent"]), account_public)
        else: browser=device=None
        session=generate(50)
        db.insert_data("session", {"user": link["user_id"], "token_hash": hash_token(session), "id": generate(), "browser": browser, "device": device, "logged_in_at": timestamp(), "next_challenge": timestamp()+3600})
        b_public, error_resp=public_key_open(link["b_public"])
        if error_resp: return error_resp
        link["session_enc"]=rsa_encrypt(b_public, session)
        link["blob"]=request.form["blob"]
        link["status"]="approved"
    return jsonify({"success": True})

@auth_bp.route("/devicelink/reject", methods=["POST"])
@sliding_window_rate_limiter(limit=20, window=120, user_limit=10)
@validate_request_data({"code": {"len": 20}})
@logged_in()
def devicelink_reject(id):
    code=request.form["code"]
    with device_links_lock:
        if code not in device_links or device_links[code]["expire"]<timestamp(): return make_json_error(400, "Invalid code")
        link=device_links[code]
        if link["user_id"]!=id: return make_json_error(403, "Not your device link")
        if link["status"]=="approved": return make_json_error(400, "Code already approved")
        link["status"]="rejected"
    return jsonify({"success": True})

@auth_bp.route("/devicelink/result")
@sliding_window_rate_limiter(limit=120, window=60)
@validate_request_data({"code": {"len": 20}}, source="args")
@pass_db
def devicelink_result(db:SQLite):
    code=request.args["code"]
    with device_links_lock:
        if code not in device_links or device_links[code]["expire"]<timestamp(): return jsonify({"status": "expired"})
        link=device_links[code]
        if link["status"]!="approved": return jsonify({"status": link["status"]})
        user_data=db.execute_raw_sql("SELECT username, public_key FROM users WHERE id=?", (link["user_id"],))
        if not user_data: return make_json_error(400, "User not found")
        result={"status": "approved", "session_enc": link["session_enc"], "blob": link["blob"], "username": user_data[0]["username"], "public": user_data[0]["public_key"]}
        del device_links[code]
    return jsonify(result)
