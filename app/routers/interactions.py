import json
from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, perm, has_permission
from app.config import generate
from app.db import SQLite
from app.routers.stream import emit

interactions_bp = Router("interactions")

def _bot_commands_for_channel(db, channel_id):
    """All commands from bots that are members of this channel."""
    rows = db.execute_raw_sql("""
        SELECT bc.id, bc.bot_id, bc.name, bc.description,
               u.username AS bot_username, u.display_name AS bot_display, u.pfp AS bot_pfp
        FROM bot_commands bc
        JOIN users u ON bc.bot_id=u.id
        JOIN members m ON m.user_id=bc.bot_id AND m.channel_id=?
        ORDER BY u.username, bc.name
    """, (channel_id,))
    return rows

@interactions_bp.route("/channel/<string:channel_id>/commands")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def get_channel_commands(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    cmd_rows = _bot_commands_for_channel(db, channel_id)
    commands = []
    for r in cmd_rows:
        opts = db.execute_raw_sql(
            "SELECT name, description, type, required, min_value, max_value, min_length, max_length, choices FROM bot_command_options WHERE command_id=? ORDER BY position",
            (r["id"],)
        )
        for o in opts:
            if o["choices"]: o["choices"] = json.loads(o["choices"])
        commands.append({
            "id": r["id"],
            "bot_id": r["bot_id"],
            "bot_username": r["bot_username"],
            "bot_display": r["bot_display"],
            "bot_pfp": r["bot_pfp"],
            "name": r["name"],
            "description": r["description"],
            "options": opts,
        })
    return jsonify({"commands": commands})

@interactions_bp.route("/channel/<string:channel_id>/interactions", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=20)
def create_interaction(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    body = request.json
    if not body or not isinstance(body, dict): return make_json_error(400, "Missing body")
    bot_id = body.get("bot_id")
    cmd_name = body.get("command")
    options_input = body.get("options") or {}
    if not bot_id or not cmd_name: return make_json_error(400, "Missing bot_id or command")
    if not isinstance(options_input, dict): return make_json_error(400, "options must be an object")
    if not db.exists("members", {"user_id": bot_id, "channel_id": channel_id}): return make_json_error(404, "Bot is not in this channel")
    cmd = db.select_data("bot_commands", ["id", "name", "description"], {"bot_id": bot_id, "name": cmd_name})
    if not cmd: return make_json_error(404, "Command not found")
    cmd_id = cmd[0]["id"]
    opts = db.execute_raw_sql(
        "SELECT name, type, required, min_value, max_value, min_length, max_length, choices FROM bot_command_options WHERE command_id=? ORDER BY position",
        (cmd_id,)
    )
    validated = {}
    for opt in opts:
        oname = opt["name"]
        otype = opt["type"]
        raw = options_input.get(oname)
        if raw is None or raw=="":
            if opt["required"]: return make_json_error(400, f"Missing required option: {oname}")
            continue
        choices = json.loads(opt["choices"]) if opt["choices"] else None
        if otype=="string":
            if not isinstance(raw, str): raw = str(raw)
            if opt["min_length"] and len(raw)<opt["min_length"]: return make_json_error(400, f"Option {oname!r} is too short (min {opt['min_length']})")
            if opt["max_length"] and len(raw)>opt["max_length"]: return make_json_error(400, f"Option {oname!r} is too long (max {opt['max_length']})")
            if choices and raw not in [c["value"] for c in choices]: return make_json_error(400, f"Option {oname!r} must be one of the allowed choices")
            validated[oname] = raw
        elif otype=="integer":
            try: v = int(raw)
            except (TypeError, ValueError): return make_json_error(400, f"Option {oname!r} must be an integer")
            if opt["min_value"] is not None and v<opt["min_value"]: return make_json_error(400, f"Option {oname!r} must be >= {opt['min_value']}")
            if opt["max_value"] is not None and v>opt["max_value"]: return make_json_error(400, f"Option {oname!r} must be <= {opt['max_value']}")
            if choices and v not in [c["value"] for c in choices]: return make_json_error(400, f"Option {oname!r} must be one of the allowed choices")
            validated[oname] = v
        elif otype=="number":
            try: v = float(raw)
            except (TypeError, ValueError): return make_json_error(400, f"Option {oname!r} must be a number")
            if opt["min_value"] is not None and v<opt["min_value"]: return make_json_error(400, f"Option {oname!r} must be >= {opt['min_value']}")
            if opt["max_value"] is not None and v>opt["max_value"]: return make_json_error(400, f"Option {oname!r} must be <= {opt['max_value']}")
            validated[oname] = v
        elif otype=="boolean":
            if isinstance(raw, bool): validated[oname] = raw
            elif str(raw).lower() in ("true","1","yes"): validated[oname] = True
            elif str(raw).lower() in ("false","0","no"): validated[oname] = False
            else: return make_json_error(400, f"Option {oname!r} must be a boolean")
        elif otype=="user":
            if not db.exists("members", {"user_id": raw, "channel_id": channel_id}): return make_json_error(400, f"Option {oname!r}: user is not in this channel")
            validated[oname] = raw
        elif otype=="channel":
            if not db.exists("members", {"user_id": id, "channel_id": raw}): return make_json_error(400, f"Option {oname!r}: you are not a member of that channel")
            validated[oname] = raw
    user_data = db.execute_raw_sql("SELECT id, username, display_name FROM users WHERE id=?", (id,))[0]
    bot_data = db.select_data("users", ["username"], {"id": bot_id})[0]
    interaction_id = generate()
    now = timestamp(True)
    db.insert_data("interaction_history", {"id": interaction_id, "channel_id": channel_id, "user_username": user_data["username"], "user_display": user_data["display_name"], "command": cmd_name, "bot_username": bot_data["username"], "timestamp": now})
    emit("interaction_create", {
        "id": interaction_id,
        "channel_id": channel_id,
        "user": {"id": user_data["id"], "username": user_data["username"], "display": user_data["display_name"]},
        "command": cmd_name,
        "options": validated,
    }, {"user_id": [bot_id]})
    emit("interaction_used", {
        "channel_id": channel_id,
        "interaction_id": interaction_id,
        "user": {"username": user_data["username"], "display": user_data["display_name"]},
        "command": cmd_name,
        "bot_username": bot_data["username"],
        "timestamp": now,
    }, {"channel_ids": [channel_id]})
    return jsonify({"success": True, "id": interaction_id})

@interactions_bp.route("/channel/<string:channel_id>/interaction-history")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def get_interaction_history(db:SQLite, id, channel_id):
    member = db.select_data("members", ["interaction_seq"], {"user_id": id, "channel_id": channel_id})
    if not member: return make_json_error(404, "Channel not found")
    history = db.execute_raw_sql("SELECT id, user_username, user_display, command, bot_username, timestamp FROM interaction_history WHERE channel_id=? AND seq>? ORDER BY timestamp DESC LIMIT 100", (channel_id, member[0]["interaction_seq"]))
    return jsonify({"history": history})

@interactions_bp.route("/channel/<string:channel_id>/interaction-history/<string:entry_id>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=20)
def delete_interaction_history(db:SQLite, id, channel_id, entry_id):
    member=db.execute_raw_sql("SELECT m.permissions, c.permissions as channel_permissions, u.username FROM members m JOIN channels c ON c.id=m.channel_id JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND m.channel_id=?", (id, channel_id))
    if not member: return make_json_error(404, "Channel not found")
    entry=db.select_data("interaction_history", ["id", "user_username"], {"id": entry_id, "channel_id": channel_id})
    if not entry: return make_json_error(404, "Entry not found")
    can_delete=entry[0]["user_username"]==member[0]["username"] or has_permission(member[0]["permissions"], perm.manage_messages, member[0]["channel_permissions"])
    if not can_delete: return make_json_error(403, "No permission to delete this entry")
    db.delete_data("interaction_history", {"id": entry_id})
    return jsonify({"success": True})
@interactions_bp.route("/channel/<string:channel_id>/messages/<string:message_id>/component", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=20)
def click_component(db:SQLite, id, channel_id, message_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    body=request.json
    if not body or not isinstance(body, dict): return make_json_error(400, "JSON body required")
    custom_id=body.get("custom_id")
    if not custom_id or not isinstance(custom_id, str): return make_json_error(400, "custom_id required")
    msg=db.execute_raw_sql("SELECT user_id, components FROM messages WHERE id=? AND channel_id=?", (message_id, channel_id))
    if not msg: return make_json_error(404, "Message not found")
    bot_id=msg[0]["user_id"]
    if not db.exists("users", {"id": bot_id, "is_bot": 1}): return make_json_error(400, "Message is not from a bot")
    components=json.loads(msg[0]["components"]) if msg[0]["components"] else []
    found=any(btn.get("type")==2 and btn.get("custom_id")==custom_id and not btn.get("disabled") for row in components if row.get("type")==1 for btn in row.get("components", []))
    if not found: return make_json_error(404, "Button not found or disabled")
    interaction_id=generate()
    now=timestamp(True)
    db.insert_data("component_interactions", {"id": interaction_id, "channel_id": channel_id, "message_id": message_id, "user_id": id, "bot_id": bot_id, "custom_id": custom_id, "timestamp": now, "responded": 0})
    user_data=db.execute_raw_sql("SELECT username, display_name AS display FROM users WHERE id=?", (id,))[0]
    emit("component_interaction", {"interaction_id": interaction_id, "channel_id": channel_id, "message_id": message_id, "custom_id": custom_id, "user": {"id": id, "username": user_data["username"], "display": user_data["display"]}}, {"user_id": [bot_id]})
    return jsonify({"interaction_id": interaction_id, "success": True})
@interactions_bp.route("/interactions/<string:interaction_id>/respond", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def respond_to_interaction(db:SQLite, id, interaction_id):
    if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "Bot only")
    row=db.select_data("component_interactions", ["user_id", "bot_id", "responded"], {"id": interaction_id})
    if not row: return make_json_error(404, "Interaction not found")
    if row[0]["bot_id"]!=id: return make_json_error(403, "Not your interaction")
    if row[0]["responded"]: return make_json_error(400, "Already responded")
    body=request.json
    if not isinstance(body, dict): return make_json_error(400, "JSON body required")
    resp_type=body.get("type")
    if resp_type not in ("ack", "modal"): return make_json_error(400, "type must be 'ack' or 'modal'")
    db.update_data("component_interactions", {"responded": 1}, {"id": interaction_id})
    if resp_type=="modal":
        title=str(body.get("title") or "")[:45]
        if not title: return make_json_error(400, "modal title required")
        modal_components=body.get("components", [])
        if not isinstance(modal_components, list) or not modal_components: return make_json_error(400, "modal components required")
        emit("interaction_modal", {"interaction_id": interaction_id, "title": title, "custom_id": str(body.get("custom_id") or ""), "components": modal_components}, {"user_id": [row[0]["user_id"]]})
    return jsonify({"success": True})
@interactions_bp.route("/interactions/<string:interaction_id>/modal-submit", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def submit_modal(db:SQLite, id, interaction_id):
    row=db.select_data("component_interactions", ["user_id", "bot_id"], {"id": interaction_id})
    if not row: return make_json_error(404, "Interaction not found")
    if row[0]["user_id"]!=id: return make_json_error(403, "Not your interaction")
    body=request.json
    if not isinstance(body, dict): return make_json_error(400, "JSON body required")
    values=body.get("components", {})
    if not isinstance(values, dict): return make_json_error(400, "components must be an object")
    user_data=db.execute_raw_sql("SELECT username, display_name AS display FROM users WHERE id=?", (id,))[0]
    emit("modal_submit", {"interaction_id": interaction_id, "user": {"id": id, "username": user_data["username"], "display": user_data["display"]}, "components": values}, {"user_id": [row[0]["bot_id"]]})
    return jsonify({"success": True})
