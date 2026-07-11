from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, perm, has_permission
from app.config import generate, config
from app.routers.stream import reaction_add, reaction_remove
from app.db import SQLite

reactions_bp=Router("reactions")

@reactions_bp.route("/channel/<string:channel_id>/message/<string:message_id>/reactions", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=150, window=60, user_limit=50)
def add_reaction(db:SQLite, id, channel_id, message_id):
    reactions_config=config.get("reactions", {})
    if not reactions_config.get("enabled", True): return make_json_error(403, "Reactions are disabled on this instance")
    member_data=db.select_data("members", ["permissions"], {"user_id": id, "channel_id": channel_id})
    if not member_data: return make_json_error(404, "Channel not found")
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data: return make_json_error(404, "Channel not found")
    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]
    user_permissions=member_data[0]["permissions"]
    if not has_permission(user_permissions, perm.add_reactions, channel_permissions): return make_json_error(403, "You don't have permission to react in this channel")
    if not db.exists("messages", {"id": message_id, "channel_id": channel_id}): return make_json_error(404, "Message not found")
    body=request.get_json(silent=True) or {}
    content=body.get("content")
    if not content or not isinstance(content, str) or len(content)>200: return make_json_error(400, "Invalid content parameter")
    if "timestamp" not in body or "signature" not in body: return make_json_error(400, "timestamp and signature are required")
    try: signed_timestamp=int(body["timestamp"])
    except (ValueError, TypeError): return make_json_error(400, "Invalid timestamp format")
    signature=body["signature"]
    if not isinstance(signature, str) or not signature: return make_json_error(400, "Invalid signature parameter")
    if abs(timestamp()-signed_timestamp)>config["messages"]["signature_timestamp_window"]: return make_json_error(400, "Timestamp is invalid")
    key=None
    iv=None
    if channel_type!=3:
        key=body.get("key")
        iv=body.get("iv")
        if not key or not iv: return make_json_error(400, "key and iv is required in non-broadcast channels")
        if len(iv)!=16: return make_json_error(400, "Invalid iv parameter, error: length")
    max_per_message=reactions_config.get("max_per_message", 20)
    existing_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM message_reactions WHERE message_id=? AND user_id=?", (message_id, id))[0]["count"]
    if existing_count>=max_per_message: return make_json_error(400, "Reaction limit reached for this message")
    if channel_type==3 and db.exists("message_reactions", {"message_id": message_id, "user_id": id, "content": content}): return make_json_error(409, "You already added this reaction")
    max_unique_per_message=reactions_config.get("max_unique_per_message", 20)
    if channel_type==3:
        unique_count=db.execute_raw_sql("SELECT COUNT(DISTINCT content) as count FROM message_reactions WHERE message_id=?", (message_id,))[0]["count"]
    else:
        unique_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM message_reactions WHERE message_id=?", (message_id,))[0]["count"]
    if unique_count>=max_unique_per_message: return make_json_error(400, "This message has reached the maximum number of distinct reactions")
    reaction_id=generate()
    created_at=timestamp(True)
    db.insert_data("message_reactions", {"id": reaction_id, "message_id": message_id, "channel_id": channel_id, "user_id": id, "content": content, "key": key, "iv": iv, "signature": signature, "signed_timestamp": signed_timestamp, "created_at": created_at})
    user_data=db.execute_raw_sql("SELECT username, display_name AS display, pfp FROM users WHERE id=?", (id,))[0]
    reaction_data={"id": reaction_id, "content": content, "key": key, "iv": iv, "signature": signature, "signed_timestamp": signed_timestamp, "created_at": created_at, "user": dict(user_data)}
    reaction_add(channel_id, message_id, reaction_data, db)
    return jsonify({"id": reaction_id, "success": True}), 201

@reactions_bp.route("/channel/<string:channel_id>/message/<string:message_id>/reactions/<string:reaction_id>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=150, window=60, user_limit=50)
def remove_reaction(db:SQLite, id, channel_id, message_id, reaction_id):
    member_data=db.select_data("members", ["permissions"], {"user_id": id, "channel_id": channel_id})
    if not member_data: return make_json_error(404, "Channel not found")
    channel_data=db.select_data("channels", ["permissions"], {"id": channel_id})
    if not channel_data: return make_json_error(404, "Channel not found")
    reaction_data=db.select_data("message_reactions", ["user_id"], {"id": reaction_id, "message_id": message_id, "channel_id": channel_id})
    if not reaction_data: return make_json_error(404, "Reaction not found")
    user_permissions=member_data[0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if reaction_data[0]["user_id"]!=id and not has_permission(user_permissions, perm.manage_messages, channel_permissions): return make_json_error(403, "You can only remove your own reactions")
    db.delete_data("message_reactions", {"id": reaction_id})
    reaction_remove(channel_id, message_id, reaction_id, db)
    return jsonify({"success": True})
