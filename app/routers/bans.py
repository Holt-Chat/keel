from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import (
    make_json_error, logged_in, sliding_window_rate_limiter, timestamp, get_args_int,
    perm, has_permission, get_pagination_params
)
from app.db import SQLite

bans_bp=Router("bans")

@bans_bp.route("/channel/<string:channel_id>/bans")
@logged_in()
@sliding_window_rate_limiter(limit=100, window=60, user_limit=30)
def get_bans(db:SQLite, id, channel_id):
    perm_data=db.get_permission_data(id, channel_id)
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    channel_data=perm_data["channel_data"]
    if channel_data[0]["type"]==1: return make_json_error(400, "Cannot manage bans for DM channels")
    admin_permissions=perm_data["admin_member"][0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if not has_permission(admin_permissions, perm.manage_members, channel_permissions): return make_json_error(403, "Member management privileges required")
    pagination=get_pagination_params()
    if isinstance(pagination, tuple): return pagination
    page_size, offset=pagination["page_size"], pagination["offset"]
    bans=db.execute_raw_sql("""
        SELECT u.id, u.username, u.display_name AS display, u.pfp, b.banned_by,
               banned_by_user.username as banned_by_username, banned_by_user.display_name as banned_by_display, b.banned_at, b.reason
        FROM bans b
        JOIN users u ON b.user_id=u.id
        JOIN users banned_by_user ON b.banned_by=banned_by_user.id
        WHERE b.channel_id=?
        ORDER BY b.seq DESC
        LIMIT ? OFFSET ?
        """, (channel_id, page_size, offset))
    return jsonify(bans)

@bans_bp.route("/channel/<string:channel_id>/bans/<string:target_username>", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
def ban_member(db:SQLite, id, channel_id, target_username):
    perm_data=db.validate_user_action(id, channel_id, target_username, "ban")
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["target_user"]: return make_json_error(404, "User not found")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    target_user_id=perm_data["target_user_id"]
    channel_data=perm_data["channel_data"]
    if channel_data[0]["type"]==1: return make_json_error(400, "Cannot ban members from DM channels")
    admin_permissions=perm_data["admin_member"][0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if not has_permission(admin_permissions, perm.manage_members, channel_permissions): return make_json_error(403, "Member management privileges required")
    if id==target_user_id: return make_json_error(400, "Cannot ban yourself")
    target_member=perm_data.get("target_member")
    if target_member:
        target_permissions=target_member[0]["permissions"]
        if has_permission(target_permissions, perm.owner, channel_permissions): return make_json_error(403, "Cannot ban owners")
        if has_permission(target_permissions, perm.admin, channel_permissions) and not has_permission(admin_permissions, perm.owner, channel_permissions): return make_json_error(403, "Cannot ban admins unless you are an owner")
        db.delete_data("members", {"user_id": target_user_id, "channel_id": channel_id})
    if perm_data.get("existing_ban"): return make_json_error(409, "User is already banned")
    reason=request.form.get("reason", "").strip()[:100] if "reason" in request.form else None
    db.insert_data("bans", {"user_id": target_user_id, "channel_id": channel_id, "banned_by": id, "banned_at": timestamp(), "reason": reason})
    return jsonify({"success": True})

@bans_bp.route("/channel/<string:channel_id>/bans/<string:target_username>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
def unban_member(db:SQLite, id, channel_id, target_username):
    perm_data=db.validate_user_action(id, channel_id, target_username, "ban")
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["target_user"]: return make_json_error(404, "User not found")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    target_user_id=perm_data["target_user_id"]
    channel_data=perm_data["channel_data"]
    if channel_data[0]["type"]==1: return make_json_error(400, "Cannot manage bans for DM channels")
    admin_permissions=perm_data["admin_member"][0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if not has_permission(admin_permissions, perm.manage_members, channel_permissions): return make_json_error(403, "Member management privileges required")
    if not perm_data.get("existing_ban"): return make_json_error(404, "User is not banned")
    db.delete_data("bans", {"user_id": target_user_id, "channel_id": channel_id})
    return jsonify({"success": True})