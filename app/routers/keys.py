from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import (
    make_json_error, logged_in, sliding_window_rate_limiter, timestamp,
    has_permission, perm
)
from app.config import generate
from app.db import SQLite

keys_bp=Router("keys")

@keys_bp.route("/channel/<string:channel_id>/key")
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
def channel_key_status(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    member_data=db.select_data("members", ["permissions"], {"user_id": id, "channel_id": channel_id})
    if not member_data: return make_json_error(404, "Channel not found")
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data: return make_json_error(404, "Channel not found")
    if channel_data[0]["type"]==3: return make_json_error(400, "Broadcast channels don't use E2EE")
    if not has_permission(member_data[0]["permissions"], perm.send_messages, channel_data[0]["permissions"]): return make_json_error(403, "You don't have permission to send messages to view current keys in this channel")
    current_key_info=db.execute_raw_sql(
        "SELECT cki.key_id, expires_at FROM channels_keys_info cki "
        "WHERE cki.channel_id = ? ORDER BY cki.seq DESC LIMIT 1",
        (channel_id,)
    )
    if not current_key_info: return jsonify({"key_id": None})
    if current_key_info[0]["expires_at"]<timestamp(True): return jsonify({"key_id": None})
    return jsonify({"key_id": current_key_info[0]["key_id"]})

@keys_bp.route("/channel/<string:channel_id>/key", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=10, window=60, user_limit=5)
def store_channel_keys(db:SQLite, id, channel_id):
    user_keys=request.get_json()
    if not isinstance(user_keys, dict): return make_json_error(400, "Invalid payload format")
    if not user_keys: return make_json_error(400, "Empty payload")
    member_data=db.select_data("members", ["permissions"], {"user_id": id, "channel_id": channel_id})
    if not member_data: return make_json_error(404, "Channel not found")
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data: return make_json_error(404, "Channel not found")
    channel_type=channel_data[0]["type"]
    if channel_type==3: return make_json_error(400, "Broadcast channels don't use E2EE")
    if not has_permission(member_data[0]["permissions"], perm.send_messages, channel_data[0]["permissions"]): return make_json_error(403, "You don't have permission to send messages to refresh keys in this channel")
    members=db.execute_raw_sql("SELECT u.username, m.user_id FROM users u JOIN members m ON u.id = m.user_id WHERE m.channel_id = ?", (channel_id,))
    if not members: return make_json_error(404, "Channel not found")
    user_ids={member["username"]:member["user_id"] for member in members}
    members=[member["username"] for member in members]
    for username, encrypted_key in user_keys.items():
        if not isinstance(username, str) or not isinstance(encrypted_key, str): return make_json_error(400, "Invalid username or key")
        if len(encrypted_key)!=344: return make_json_error(400, "Invalid key length")
        if username not in members: return  make_json_error(400, "User not found in the channel")
    for username in members:
        if username not in user_keys: return make_json_error(400, "User missing from keys")
    key_id=generate()
    with db:
        current_key_info=db.execute_raw_sql("SELECT cki.key_id, expires_at FROM channels_keys_info cki WHERE cki.channel_id=? ORDER BY cki.seq DESC LIMIT 1", (channel_id,))
        if current_key_info and current_key_info[0]["expires_at"]>=timestamp(True): return jsonify({"key_id": current_key_info[0]["key_id"], "success": True}), 200
        db.insert_data("channels_keys_info", {"key_id": key_id, "channel_id": channel_id, "by": id, "timestamp": timestamp(), "expires_at": timestamp(True)+86400000})
        for username, encrypted_key in user_keys.items():
            db.insert_data("channels_keys", {"id": key_id, "channel_id": channel_id, "user_id": user_ids[username], "key": encrypted_key})
    return jsonify({"key_id": key_id, "success": True}), 201

@keys_bp.route("/key/<string:key_id>")
@logged_in()
@sliding_window_rate_limiter(limit=120, window=60, user_limit=60)
def get_key(db:SQLite, id, key_id):
    result=db.execute_raw_sql("SELECT ck.key, cki.by, cki.expires_at FROM channels_keys ck JOIN channels_keys_info cki ON ck.id=cki.key_id WHERE ck.user_id=? AND ck.id=?", (id, key_id))
    if not result: return make_json_error(404, "Key not found")
    return jsonify({**result[0], "success": True})

@keys_bp.route("/keys", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def get_keys(db:SQLite, id):
    key_ids=request.get_json()
    if not isinstance(key_ids, list): return make_json_error(400, "Invalid payload format")
    if len(key_ids)>100: return make_json_error(400, "Too many keys requested")
    results=db.execute_raw_sql(f"SELECT cki.key_id, ck.key, cki.by, cki.expires_at FROM channels_keys ck JOIN channels_keys_info cki ON ck.id=cki.key_id WHERE ck.user_id=? AND ck.id IN ({','.join('?' * len(key_ids))})", [id]+key_ids)
    if not results: return make_json_error(404, "No key found")
    return jsonify(results)