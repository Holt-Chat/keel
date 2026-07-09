from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import (
    make_json_error, logged_in, sliding_window_rate_limiter,
    perm, has_permission, get_pagination_params
)
from app.routers.stream import member_leave, member_perms_changed
from app.db import SQLite

members_bp=Router("members")
PERM_BITS=perm.mask.bit_length()

@members_bp.route("/channel/<string:channel_id>/members")
@logged_in()
@sliding_window_rate_limiter(limit=100, window=60, user_limit=30)
def members(db:SQLite, id, channel_id):
    perm_data=db.get_permission_data(id, channel_id)
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    user_permissions=perm_data["admin_member"][0]["permissions"]
    channel_data=perm_data["channel_data"]
    channel_permissions=channel_data[0]["permissions"]
    if channel_data[0]["type"]==3 and not (has_permission(user_permissions, perm.manage_members, channel_permissions) or has_permission(user_permissions, perm.manage_permissions, channel_permissions)): return make_json_error(403, "You don't have permission to view members")
    pb_mode="pb" in request.args
    if pb_mode:
        if channel_data[0]["type"]==3:
            channel_members=db.execute_raw_sql("""
                SELECT u.username, u.public_key AS public
                FROM users u
                JOIN members m ON u.id=m.user_id
                WHERE m.channel_id=? AND ((m.permissions&?)!=0 OR (m.permissions&?)!=0)
                """, (channel_id, perm.manage_members, perm.manage_permissions))
        else:
            channel_members=db.execute_raw_sql("""
                SELECT u.username, u.public_key AS public
                FROM users u
                JOIN members m ON u.id=m.user_id
                WHERE m.channel_id=?
                """, (channel_id,))
        return jsonify(channel_members)
    pagination=get_pagination_params()
    if isinstance(pagination, tuple): return pagination
    page_size, offset=pagination["page_size"], pagination["offset"]
    if has_permission(user_permissions, perm.manage_permissions, channel_permissions):
        channel_members=db.execute_raw_sql("""
            SELECT u.id, u.username, u.display_name AS display, u.pfp, u.is_bot,
                   CASE WHEN m.permissions IS NULL THEN ? ELSE m.permissions END as permissions,
                   m.joined_at
            FROM users u
            JOIN members m ON u.id=m.user_id
            WHERE m.channel_id=?
            ORDER BY u.username
            LIMIT ? OFFSET ?
            """, (channel_permissions, channel_id, page_size, offset))
    else:
        channel_members=db.execute_raw_sql("""
            SELECT u.id, u.username, u.display_name AS display, u.pfp, u.is_bot, m.joined_at
            FROM users u
            JOIN members m ON u.id=m.user_id
            WHERE m.channel_id=?
            ORDER BY u.username
            LIMIT ? OFFSET ?
        """, (channel_id, page_size, offset))
    return jsonify(channel_members)

@members_bp.route("/channel/<string:channel_id>/member/<string:target_username>", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
def kick_member(db:SQLite, id, channel_id, target_username):
    perm_data=db.validate_user_action(id, channel_id, target_username, "kick")
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["target_user"]: return make_json_error(404, "User not found")
    if not perm_data["target_member"]: return make_json_error(404, "User not found in channel")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    target_user_id=perm_data["target_user_id"]
    channel_data=perm_data["channel_data"]
    if channel_data[0]["type"]==1: return make_json_error(400, "Cannot kick members from DM channels")
    admin_permissions=perm_data["admin_member"][0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if not has_permission(admin_permissions, perm.manage_members, channel_permissions): return make_json_error(403, "Member management privileges required")
    target_permissions=perm_data["target_member"][0]["permissions"]
    if id==target_user_id: return make_json_error(400, "Cannot kick yourself")
    if has_permission(target_permissions, perm.owner, channel_permissions) and not has_permission(admin_permissions, perm.owner, channel_permissions): return make_json_error(403, "Cannot kick owners unless you are an owner")
    if has_permission(target_permissions, perm.admin, channel_permissions) and not has_permission(admin_permissions, perm.owner, channel_permissions): return make_json_error(403, "Cannot kick admins unless you are an owner")

    user_data=db.execute_raw_sql("""
        SELECT u.id, u.username, u.display_name, u.pfp
        FROM users u
        WHERE u.id=?
    """, (target_user_id,))[0]

    db.delete_data("members", {"user_id": target_user_id, "channel_id": channel_id})

    # Emit member leave event
    member_leave(channel_id, user_data, db)

    return jsonify({"success": True})

@members_bp.route("/channel/<string:channel_id>/member/<string:target_username>", methods=["PATCH"])
@logged_in()
@sliding_window_rate_limiter(limit=50, window=60, user_limit=20)
def manage_members(db:SQLite, id, channel_id, target_username):
    new_permissions=request.get_json()
    if "permissions" not in new_permissions: return make_json_error(400, "permissions parameter is missing")
    new_permissions=new_permissions["permissions"]
    if isinstance(new_permissions, int): new_permissions=new_permissions&perm.mask
    else: return make_json_error(400, "Invalid permissions format")
    perm_data=db.validate_user_action(id, channel_id, target_username)
    if not perm_data["admin_member"]: return make_json_error(404, "Channel not found")
    if not perm_data["target_user"]: return make_json_error(404, "User not found")
    if not perm_data["target_member"]: return make_json_error(404, "User not found in channel")
    if not perm_data["channel_data"]: return make_json_error(404, "Channel not found")
    target_user_id=perm_data["target_user_id"]
    channel_data=perm_data["channel_data"]
    if channel_data[0]["type"]==1: return make_json_error(400, "Cannot manage permissions in DM channels")
    admin_permissions=perm_data["admin_member"][0]["permissions"]
    channel_permissions=channel_data[0]["permissions"]
    if not has_permission(admin_permissions, perm.manage_permissions, channel_permissions):
        return make_json_error(403, "Insufficient permissions")
    target_permissions=perm_data["target_member"][0]["permissions"]
    if not has_permission(admin_permissions, perm.owner, channel_permissions):
        for bit in range(PERM_BITS):
            permission_bit=1<<bit
            if (new_permissions&permission_bit) and not has_permission(admin_permissions, permission_bit, channel_permissions):
                return make_json_error(403, "Cannot assign permissions you don't have")
    if id==target_user_id:
        if has_permission(target_permissions, perm.owner, channel_permissions):
            if not has_permission(new_permissions, perm.owner, channel_permissions):
                cursor=db.execute("""
                    UPDATE members 
                    SET permissions = ?
                    WHERE channel_id = ? AND user_id = ? 
                    AND (SELECT COUNT(*) FROM members WHERE channel_id = ? AND (permissions & 1) = 1) > 1
                """, (new_permissions, channel_id, target_user_id, channel_id))
                if cursor.rowcount==0: return make_json_error(403, "Cannot remove owner permission as the last owner")
                db.commit()
        db.update_data("members", {"permissions": new_permissions}, {"user_id": target_user_id, "channel_id": channel_id})
    else:
        if has_permission(target_permissions, perm.owner, channel_permissions) and not has_permission(admin_permissions, perm.owner, channel_permissions): return make_json_error(403, "Only owners can modify other owners")
        if has_permission(target_permissions, perm.admin, channel_permissions) and not has_permission(admin_permissions, perm.owner, channel_permissions): return make_json_error(403, "Only owners can modify admins")
        db.update_data("members", {"permissions": new_permissions}, {"user_id": target_user_id, "channel_id": channel_id})
    member_perms_changed(channel_id, target_user_id, target_username, new_permissions, db)
    return jsonify({"success": True})