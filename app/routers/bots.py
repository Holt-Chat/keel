import re
import json
from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import (
    make_json_error, logged_in, sliding_window_rate_limiter, validate_request_data,
    timestamp, hash_token, public_key_open, handle_pfp
)
from app.config import generate, config
from app.db import SQLite
from app.routers.stream import member_info_changed

VALID_OPTION_TYPES = {"string", "integer", "number", "boolean", "user", "channel"}
CMD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

def _validate_command(cmd):
    """Validate a command dict. Returns error string or None."""
    if not isinstance(cmd, dict): return "Command must be an object"
    name = cmd.get("name", "")
    if not CMD_NAME_RE.match(name): return f"Invalid command name: {name!r}"
    desc = cmd.get("description", "")
    if not isinstance(desc, str) or not 1<=len(desc)<=100: return f"Description must be 1-100 chars for command {name!r}"
    options = cmd.get("options", [])
    if not isinstance(options, list): return f"Options must be a list for command {name!r}"
    seen_names = set()
    for i, opt in enumerate(options):
        if not isinstance(opt, dict): return f"Option {i} of {name!r} must be an object"
        oname = opt.get("name", "")
        if not CMD_NAME_RE.match(oname): return f"Invalid option name {oname!r} in {name!r}"
        if oname in seen_names: return f"Duplicate option name {oname!r} in {name!r}"
        seen_names.add(oname)
        odesc = opt.get("description", "")
        if not isinstance(odesc, str) or not 1<=len(odesc)<=100: return f"Option {oname!r} description must be 1-100 chars"
        otype = opt.get("type", "")
        if otype not in VALID_OPTION_TYPES: return f"Invalid type {otype!r} for option {oname!r}"
        choices = opt.get("choices")
        if choices is not None:
            if not isinstance(choices, list) or len(choices)>25: return f"Choices for {oname!r} must be a list of up to 25"
            for c in choices:
                if not isinstance(c, dict) or "name" not in c or "value" not in c: return f"Each choice must have name and value in {oname!r}"
        if otype in ("integer", "number"):
            mn, mx = opt.get("min_value"), opt.get("max_value")
            if mn is not None and not isinstance(mn, (int, float)): return f"min_value must be numeric for {oname!r}"
            if mx is not None and not isinstance(mx, (int, float)): return f"max_value must be numeric for {oname!r}"
            if mn is not None and mx is not None and mn>mx: return f"min_value > max_value for {oname!r}"
        if otype=="string":
            mnl, mxl = opt.get("min_length"), opt.get("max_length")
            if mnl is not None and not isinstance(mnl, int): return f"min_length must be int for {oname!r}"
            if mxl is not None and not isinstance(mxl, int): return f"max_length must be int for {oname!r}"
            if mnl is not None and mxl is not None and mnl>mxl: return f"min_length > max_length for {oname!r}"
    return None

def _upsert_command(db, bot_id, cmd):
    name = cmd["name"]
    desc = cmd["description"]
    options = cmd.get("options", [])
    existing = db.select_data("bot_commands", ["id"], {"bot_id": bot_id, "name": name})
    if existing:
        cmd_id = existing[0]["id"]
        db.update_data("bot_commands", {"description": desc}, {"id": cmd_id})
        db.delete_data("bot_command_options", {"command_id": cmd_id})
    else:
        cmd_id = generate()
        db.insert_data("bot_commands", {"id": cmd_id, "bot_id": bot_id, "name": name, "description": desc})
    for i, opt in enumerate(options):
        choices = opt.get("choices")
        db.insert_data("bot_command_options", {
            "id": generate(),
            "command_id": cmd_id,
            "name": opt["name"],
            "description": opt["description"],
            "type": opt["type"],
            "required": 1 if opt.get("required") else 0,
            "min_value": opt.get("min_value"),
            "max_value": opt.get("max_value"),
            "min_length": opt.get("min_length"),
            "max_length": opt.get("max_length"),
            "choices": json.dumps(choices) if choices else None,
            "position": i,
        })
    return cmd_id

def _command_with_options(db, cmd_id, name, description):
    opts = db.execute_raw_sql(
        "SELECT name, description, type, required, min_value, max_value, min_length, max_length, choices FROM bot_command_options WHERE command_id=? ORDER BY position",
        (cmd_id,)
    )
    for o in opts:
        if o["choices"]: o["choices"] = json.loads(o["choices"])
    return {"id": cmd_id, "name": name, "description": description, "options": opts}

bots_bp=Router("bots")
username_regex=re.compile(r"[a-z0-9_\-]+")

def _bot_summary(db, bot_id):
    bot=db.select_data("users", ["id", "username", "display_name AS display", "pfp", "public_key AS public", "bot_private AS private", "created_at"], {"id": bot_id})
    return bot[0] if bot else None

@bots_bp.route("/bots")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def list_bots(db:SQLite, id):
    bots=db.select_data("users", ["id", "username", "display_name AS display", "pfp", "public_key AS public", "bot_private AS private", "created_at"], {"owner_id": id}, "created_at DESC")
    return jsonify(bots)

@bots_bp.route("/bots", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=10, window=300, user_limit=5)
@validate_request_data({"username": {"minlen": 3, "maxlen": 20, "regex": username_regex}, "public": {"len": 392}}, 400)
def create_bot(db:SQLite, id):
    max_bots=config["max_members"].get("max_bots", 10)
    bot_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM users WHERE owner_id=? AND is_bot=1", (id,))[0]["count"]
    if bot_count>=max_bots: return make_json_error(403, "You have reached the maximum number of bots")
    display=request.form.get("display", "").strip()
    if display and (len(display)<2 or len(display)>24): return make_json_error(400, "Invalid display parameter, error: length")
    if db.exists("users", {"username": request.form["username"]}): return make_json_error(400, "Username is in use")
    public_key, error_resp=public_key_open()
    if error_resp: return error_resp
    pfp_result=handle_pfp(db=db)
    if isinstance(pfp_result, tuple): return pfp_result
    bot_id=generate()
    token=generate(30)
    now=timestamp()
    try:
        db.insert_data("users", {"id": bot_id, "username": request.form["username"], "display_name": display or None, "pfp": pfp_result, "passkey": "bot", "public_key": request.form["public"], "created_at": now, "is_bot": 1, "owner_id": id})
    except Exception as e:
        if "UNIQUE constraint failed" in str(e): return make_json_error(400, "Username is in use")
        raise
    db.insert_data("bot_tokens", {"id": generate(), "bot_id": bot_id, "token_hash": hash_token(token), "created_at": now})
    return jsonify({"bot": _bot_summary(db, bot_id), "token": token, "success": True}), 201

@bots_bp.route("/bots/me")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def bot_me(db:SQLite, id):
    bot=db.select_data("users", ["id", "username", "display_name AS display", "pfp", "public_key AS public", "bot_private AS private", "owner_id", "is_bot", "created_at"], {"id": id})
    if not bot or not bot[0]["is_bot"]: return make_json_error(403, "This endpoint is only available to bots")
    return jsonify({**bot[0], "success": True})

@bots_bp.route("/bot/<string:bot_id>", methods=["PATCH"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def edit_bot(db:SQLite, id, bot_id):
    if not db.exists("users", {"id": bot_id, "owner_id": id, "is_bot": 1}): return make_json_error(404, "Bot not found")
    update_data={}
    errors=[]
    if "private" in request.form:
        private_value=request.form["private"]
        update_data["bot_private"]=1 if private_value in (True, "1", "true", "True") else 0
    if "display" in request.form:
        if request.form["display"]=="": update_data["display_name"]=None
        elif len(request.form["display"])>1 and len(request.form["display"])<25: update_data["display_name"]=request.form["display"]
        else: errors.append("Invalid display parameter, error: length")
    if request.files and "pfp" in request.files:
        pfp_result=handle_pfp(error_as_text=True, db=db)
        if not isinstance(pfp_result, tuple):
            if pfp_result:
                old_pfp_data=db.execute_raw_sql("SELECT pfp FROM users WHERE id=?", (bot_id,))
                old_pfp_id=old_pfp_data[0]["pfp"] if old_pfp_data and old_pfp_data[0]["pfp"] else None
                if old_pfp_id!=pfp_result: update_data["pfp"]=pfp_result
                else: errors.append("Profile picture is the same")
        else: errors.append(pfp_result[0])
    elif request.form.get("remove_pfp")=="1":
        old_pfp_data=db.execute_raw_sql("SELECT pfp FROM users WHERE id=?", (bot_id,))
        old_pfp_id=old_pfp_data[0]["pfp"] if old_pfp_data and old_pfp_data[0]["pfp"] else None
        if old_pfp_id: update_data["pfp"]=None
    if not update_data: return jsonify({"error": "No valid parameters to update", "errors": errors, "success": False}), 400
    db.update_data("users", update_data, {"id": bot_id})
    if "pfp" in update_data: db.cleanup_unused_files()
    updated_bot=db.select_data("users", ["id", "username", "display_name AS display", "pfp"], {"id": bot_id})[0]
    member_info_changed(bot_id, updated_bot, db)
    return jsonify({"bot": updated_bot, "errors": errors, "success": True})

@bots_bp.route("/bot/<string:bot_id>/token", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=10, window=300, user_limit=5)
def regenerate_token(db:SQLite, id, bot_id):
    if not db.exists("users", {"id": bot_id, "owner_id": id, "is_bot": 1}): return make_json_error(404, "Bot not found")
    token=generate(30)
    db.delete_data("bot_tokens", {"bot_id": bot_id})
    db.insert_data("bot_tokens", {"id": generate(), "bot_id": bot_id, "token_hash": hash_token(token), "created_at": timestamp()})
    return jsonify({"token": token, "success": True}), 201

@bots_bp.route("/bot/<string:bot_id>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def delete_bot(db:SQLite, id, bot_id):
    bot=db.select_data("users", ["pfp"], {"id": bot_id, "owner_id": id, "is_bot": 1})
    if not bot: return make_json_error(404, "Bot not found")
    db.delete_data("users", {"id": bot_id})
    if bot[0]["pfp"]: db.cleanup_unused_files()
    db.cleanup_unused_files()
    db.cleanup_unused_keys()
    return jsonify({"success": True})

@bots_bp.route("/bots/me/commands")
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def list_my_commands(db:SQLite, id):
    if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "Bot only")
    rows = db.execute_raw_sql("SELECT id, name, description FROM bot_commands WHERE bot_id=? ORDER BY name", (id,))
    return jsonify([_command_with_options(db, r["id"], r["name"], r["description"]) for r in rows])

@bots_bp.route("/bots/me/commands", methods=["PUT"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def replace_all_commands(db:SQLite, id):
    if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "Bot only")
    body = request.json
    if not isinstance(body, list): return make_json_error(400, "Body must be an array of commands")
    if len(body)>100: return make_json_error(400, "Maximum 100 commands")
    for cmd in body:
        err = _validate_command(cmd)
        if err: return make_json_error(400, err)
    names = [c["name"] for c in body]
    if len(names)!=len(set(names)): return make_json_error(400, "Duplicate command names")
    db.delete_data("bot_commands", {"bot_id": id})
    result = [_command_with_options(db, _upsert_command(db, id, cmd), cmd["name"], cmd["description"]) for cmd in body]
    return jsonify(result)

@bots_bp.route("/bots/me/commands", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def upsert_command(db:SQLite, id):
    if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "Bot only")
    cmd = request.json
    err = _validate_command(cmd)
    if err: return make_json_error(400, err)
    count = db.execute_raw_sql("SELECT COUNT(*) as c FROM bot_commands WHERE bot_id=?", (id,))[0]["c"]
    if count>=100 and not db.exists("bot_commands", {"bot_id": id, "name": cmd["name"]}): return make_json_error(400, "Maximum 100 commands")
    cmd_id = _upsert_command(db, id, cmd)
    return jsonify(_command_with_options(db, cmd_id, cmd["name"], cmd["description"]))

@bots_bp.route("/bots/me/commands/<string:cmd_name>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def delete_command(db:SQLite, id, cmd_name):
    if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "Bot only")
    rows = db.select_data("bot_commands", ["id"], {"bot_id": id, "name": cmd_name})
    if not rows: return make_json_error(404, "Command not found")
    db.delete_data("bot_commands", {"id": rows[0]["id"]})
    return jsonify({"success": True})
