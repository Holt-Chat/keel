from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, validate_request_data, perm, has_permission as perm_check
from app.routers.stream import call_start, call_join, call_left, call_signal
from app.config import config, generate
from app.db import SQLite

calls_bp=Router("calls")

@calls_bp.route("/channel/<string:channel_id>/call", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def start_or_join_call(db:SQLite, id, channel_id):
    if not config["calls"]["enabled"]: return make_json_error(403, "Calls are disabled")
    member_channel_data=db.execute_raw_sql("""
        SELECT c.type FROM channels c
        JOIN members m ON c.id=m.channel_id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return make_json_error(404, "Channel not found")
    channel_type=member_channel_data[0]["type"]
    if channel_type==3: return make_json_error(400, "Calls are not supported in this channel")
    if channel_type==1:
        other_member=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, id))
        if other_member and db.exists("blocks", {"blocker_id": other_member[0]["user_id"], "blocked_id": id}): return make_json_error(403, "You are blocked by this user")
    active_calls=db.execute_raw_sql("""
        SELECT cp.channel_id FROM call_participants cp
        WHERE cp.user_id=? AND cp.left_at IS NULL AND cp.channel_id!=?
    """, (id, channel_id))
    if active_calls:
        for active_call in active_calls:
            db.update_data("call_participants", {"left_at": timestamp(True)}, {"channel_id": active_call["channel_id"], "user_id": id})
            user_data_leave=db.execute_raw_sql("SELECT id, username, display_name, pfp FROM users WHERE id=?", (id,))[0]
            call_left(active_call["channel_id"], user_data_leave, db)
            remaining_participants=db.execute_raw_sql("SELECT COUNT(*) as count FROM call_participants WHERE channel_id=? AND left_at IS NULL", (active_call["channel_id"],))
            if remaining_participants[0]["count"]==0:
                db.delete_data("calls", {"channel_id": active_call["channel_id"]})
    existing_call=db.select_data("calls", ["started_by", "started_at"], {"channel_id": channel_id})
    if existing_call:
        participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
        if participant and participant[0]["left_at"] is None: return make_json_error(400, "You are already in this call")
        active_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM call_participants WHERE channel_id=? AND left_at IS NULL", (channel_id,))[0]["count"]
        if active_count>=config["calls"].get("max_participants", 8): return make_json_error(403, "Call is full")
        if participant:
            db.update_data("call_participants", {"joined_at": timestamp(True), "left_at": None}, {"channel_id": channel_id, "user_id": id})
        else:
            db.insert_data("call_participants", {"channel_id": channel_id, "user_id": id, "joined_at": timestamp(True)})
        user_data=db.execute_raw_sql("SELECT id, username, display_name, pfp, public_key FROM users WHERE id=?", (id,))[0]
        db.execute_raw_sql("UPDATE call_history SET participant_count=participant_count+1 WHERE channel_id=? AND ended_at IS NULL", (channel_id,))
        call_join(channel_id, user_data, db)
        return jsonify({"success": True, "joined": True})
    ts=timestamp(True)
    db.insert_data("calls", {"channel_id": channel_id, "started_by": id, "started_at": ts})
    db.insert_data("call_participants", {"channel_id": channel_id, "user_id": id, "joined_at": ts})
    user_data=db.execute_raw_sql("SELECT username FROM users WHERE id=?", (id,))[0]
    db.insert_data("call_history", {"id": generate(), "channel_id": channel_id, "started_by": user_data["username"], "started_at": ts, "ended_at": None, "participant_count": 1})
    call_start(channel_id, user_data["username"], db)
    return jsonify({"success": True, "started": True}), 201

@calls_bp.route("/channel/<string:channel_id>/call", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def leave_call(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
    if not participant: return make_json_error(404, "You are not in this call")
    if participant[0]["left_at"] is not None: return make_json_error(400, "You already left this call")
    db.update_data("call_participants", {"left_at": timestamp(True)}, {"channel_id": channel_id, "user_id": id})
    user_data=db.execute_raw_sql("SELECT id, username, display_name, pfp FROM users WHERE id=?", (id,))[0]
    call_left(channel_id, user_data, db)
    active_participants=db.execute_raw_sql("SELECT COUNT(*) as count FROM call_participants WHERE channel_id=? AND left_at IS NULL", (channel_id,))
    if active_participants[0]["count"]==0:
        db.delete_data("calls", {"channel_id": channel_id})
        db.execute_raw_sql("UPDATE call_history SET ended_at=? WHERE channel_id=? AND ended_at IS NULL", (timestamp(True), channel_id))
    return jsonify({"success": True})

@calls_bp.route("/channel/<string:channel_id>/call/signal", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=500, window=60, user_limit=250)
@validate_request_data({"type": {}, "data": {}}, source="json")
def signal_call(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
    if not participant or participant[0]["left_at"] is not None: return make_json_error(403, "You are not in this call")
    signal_type=request.json.get("type")
    signal_data=request.json.get("data")
    target=request.json.get("target")
    if signal_type not in ["offer", "answer", "ice", "settings"]: return make_json_error(400, "Invalid signal type")
    if signal_type=="settings": target=None
    else:
        if not target: return make_json_error(400, "Missing target")
        target_participant=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": target})
        if not target_participant or target_participant[0]["left_at"] is not None: return make_json_error(404, "Target is not in this call")
    call_signal(channel_id, id, signal_type, signal_data, target, db)
    return jsonify({"success": True})

@calls_bp.route("/channel/<string:channel_id>/call", methods=["GET"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=25)
def get_call_status(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    call_data=db.select_data("calls", ["started_by", "started_at"], {"channel_id": channel_id})
    if not call_data: return jsonify({"active": False})
    requester=db.select_data("call_participants", ["left_at"], {"channel_id": channel_id, "user_id": id})
    is_participant=bool(requester) and requester[0]["left_at"] is None
    starter_data=db.execute_raw_sql("SELECT username FROM users WHERE id=?", (call_data[0]["started_by"],))[0]
    participants=db.execute_raw_sql("""
        SELECT u.id, u.username, u.display_name, u.pfp, u.public_key, cp.joined_at
        FROM call_participants cp
        JOIN users u ON cp.user_id=u.id
        WHERE cp.channel_id=? AND cp.left_at IS NULL
    """, (channel_id,))
    answered=len(participants)>=2
    return jsonify({"active": True, "answered": answered, "started_by": starter_data["username"], "started_at": call_data[0]["started_at"], "participants": [{**({"id": p["id"]} if is_participant else {}), "username": p["username"], "display": p["display_name"], "pfp": p["pfp"], "public": p["public_key"], "joined_at": p["joined_at"]} for p in participants]})

@calls_bp.route("/channel/<string:channel_id>/call-history", methods=["GET"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
def get_call_history(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    history=db.execute_raw_sql("SELECT id, started_by, started_at, ended_at, participant_count FROM call_history WHERE channel_id=? ORDER BY started_at DESC LIMIT 100", (channel_id,))
    return jsonify({"history": history})

@calls_bp.route("/channel/<string:channel_id>/call-history/<string:history_id>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=60, user_limit=10)
def delete_call_history(db:SQLite, id, channel_id, history_id):
    user_member=db.select_data("members", ["permissions"], {"user_id": id, "channel_id": channel_id})
    if not user_member: return make_json_error(404, "Channel not found")
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data: return make_json_error(404, "Channel not found")
    if channel_data[0]["type"]!=1 and not perm_check(user_member[0]["permissions"], perm.manage_messages, channel_data[0]["permissions"]): return make_json_error(403, "Insufficient permissions")
    if not db.exists("call_history", {"id": history_id, "channel_id": channel_id}): return make_json_error(404, "Not found")
    db.delete_data("call_history", {"id": history_id})
    return jsonify({"success": True})
