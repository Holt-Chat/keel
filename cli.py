#!/usr/bin/env python
import sys
import argparse
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm
from rich.panel import Panel
from rich import box
from rich.text import Text
from db import SQLite

console=Console()

def format_timestamp(timestamp):
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return "N/A"

def list_users(page=1, per_page=20):
    db=SQLite()
    offset=(page-1)*per_page
    total_query=db.execute("SELECT COUNT(*) as count FROM users")
    total=total_query.fetchone()["count"]
    total_pages=(total+per_page-1)//per_page
    users=db.select_data("users", columns=["seq", "id", "username", "display_name", "created_at"], order_by="seq ASC", limit=per_page, offset=offset)
    db.close()
    if not users:
        console.print("[yellow]No users found.[/yellow]")
        return
    table=Table(title=f"Users (Page {page}/{total_pages} - Total: {total})", box=box.ROUNDED, header_style="bold magenta")
    table.add_column("Seq", style="cyan", justify="right")
    table.add_column("ID", style="blue")
    table.add_column("Username", style="green")
    table.add_column("Display Name", style="yellow")
    table.add_column("Created At", style="white")
    for user in users:
        table.add_row(str(user["seq"]), user["id"], user["username"], user["display_name"] or "-", format_timestamp(user["created_at"]))
    console.print(table)
    console.print(f"\n[dim]Showing {len(users)} of {total} users[/dim]")

def delete_channel(channel_id):
    db=SQLite()
    channel_data=db.select_data("channels", conditions={"id": channel_id})
    if not channel_data:
        console.print(f"[red]Channel with ID '{channel_id}' not found.[/red]")
        db.close()
        return
    channel=channel_data[0]
    channel_type=channel["type"]
    members_count_query=db.execute("SELECT COUNT(*) as count FROM members WHERE channel_id=?", (channel_id,))
    members_count=members_count_query.fetchone()["count"]
    messages_count_query=db.execute("SELECT COUNT(*) as count FROM messages WHERE channel_id=?", (channel_id,))
    messages_count=messages_count_query.fetchone()["count"]
    db.close()
    if channel_type==1:
        console.print(f"[yellow]Warning: This is a DM channel. DM channels cannot be deleted, only hidden by users.[/yellow]")
        console.print(f"[yellow]Use delete-user command to remove users and their DM channels instead.[/yellow]")
        return
    panel_content=f"""[bold]Channel ID:[/bold] {channel['id']}
[bold]Name:[/bold] {channel['name'] or '(DM Channel)'}
[bold]Type:[/bold] {['', 'DM', 'Group', 'Announcement'][channel['type']]}
[bold]Members:[/bold] {members_count}
[bold]Messages:[/bold] {messages_count}
[bold]Created At:[/bold] {format_timestamp(channel['created_at'])}"""
    console.print(Panel(panel_content, title="Channel Information", border_style="yellow"))
    if not Confirm.ask(f"[bold red]Are you sure you want to delete this channel?[/bold red]"):
        console.print("[yellow]Deletion cancelled.[/yellow]")
        return
    try:
        with SQLite() as db:
            channel_pfp=db.select_data("channels", ["pfp"], {"id": channel_id})
            db.delete_data("channels", {"id": channel_id})
            if channel_pfp and channel_pfp[0]["pfp"]:
                db.cleanup_unused_files()
            db.cleanup_unused_files()
            db.cleanup_unused_keys()
        console.print(f"[green]✓ Channel '{channel_id}' has been successfully deleted.[/green]")
    except Exception as e:
        console.print(f"[red]✗ Error deleting channel: {e}[/red]")

def delete_user(username):
    db=SQLite()
    user_data=db.select_data("users", conditions={"username": username})
    if not user_data:
        console.print(f"[red]User '{username}' not found.[/red]")
        db.close()
        return
    user=user_data[0]
    user_id=user["id"]
    channels_query=db.execute("SELECT COUNT(*) as count FROM members WHERE user_id=?", (user_id,))
    channels_count=channels_query.fetchone()["count"]
    messages_query=db.execute("SELECT COUNT(*) as count FROM messages WHERE user_id=?", (user_id,))
    messages_count=messages_query.fetchone()["count"]
    owned_channels_query=db.execute("SELECT COUNT(*) as count FROM members m JOIN channels c ON m.channel_id=c.id WHERE m.user_id=? AND (m.permissions & 2)=2 AND c.type!=1", (user_id,))
    owned_channels_count=owned_channels_query.fetchone()["count"]
    db.close()
    panel_content=f"""[bold]User ID:[/bold] {user['id']}
[bold]Username:[/bold] {user['username']}
[bold]Display Name:[/bold] {user['display_name'] or '-'}
[bold]Channels:[/bold] {channels_count}
[bold]Owned Channels:[/bold] {owned_channels_count}
[bold]Messages:[/bold] {messages_count}
[bold]Created At:[/bold] {format_timestamp(user['created_at'])}"""
    console.print(Panel(panel_content, title="User Information", border_style="yellow"))
    if owned_channels_count>0:
        console.print(f"[yellow]Warning: User owns {owned_channels_count} channel(s). These will be deleted if user is the last owner.[/yellow]")
    if not Confirm.ask(f"[bold red]Are you sure you want to delete user '{username}'?[/bold red]"):
        console.print("[yellow]Deletion cancelled.[/yellow]")
        return
    try:
        with SQLite() as db:
            user_channels=db.execute_raw_sql("SELECT c.id, c.type, c.pfp, m.permissions, c.permissions as channel_permissions FROM channels c JOIN members m ON c.id=m.channel_id WHERE m.user_id=?", (user_id,))
            channels_to_delete=[]
            dm_channels_to_delete=[]
            for channel in user_channels:
                channel_id=channel["id"]
                channel_type=channel["type"]
                user_permissions=channel["permissions"]
                channel_permissions=channel["channel_permissions"]
                if channel_type==1:
                    dm_channels_to_delete.append(channel_id)
                    continue
                if (user_permissions is not None and (user_permissions & 2)==2) or (user_permissions is None and (channel_permissions & 2)==2):
                    owner_count=db.execute_raw_sql("SELECT COUNT(*) as count FROM members WHERE channel_id=? AND (permissions & 2)=2", (channel_id,))[0]["count"]
                    if owner_count==1:
                        channels_to_delete.append(channel_id)
            for channel_id in channels_to_delete:
                channel_pfp=db.select_data("channels", ["pfp"], {"id": channel_id})
                db.delete_data("channels", {"id": channel_id})
                if channel_pfp and channel_pfp[0]["pfp"]:
                    db.cleanup_unused_files()
            for channel_id in dm_channels_to_delete:
                db.delete_data("channels", {"id": channel_id})
            pfp=db.select_data("users", ["pfp"], {"id": user_id})
            db.delete_data("users", {"id": user_id})
            if pfp and pfp[0]["pfp"]:
                db.cleanup_unused_files()
            db.cleanup_unused_files()
            db.cleanup_unused_keys()
        console.print(f"[green]✓ User '{username}' has been successfully deleted.[/green]")
        if channels_to_delete:
            console.print(f"[green]✓ Deleted {len(channels_to_delete)} owned channel(s).[/green]")
        if dm_channels_to_delete:
            console.print(f"[green]✓ Deleted {len(dm_channels_to_delete)} DM channel(s).[/green]")
    except Exception as e:
        console.print(f"[red]✗ Error deleting user: {e}[/red]")

def show_help():
    help_text=Text()
    help_text.append("Holt Keel CLI - User & Channel Management\n\n", style="bold cyan")
    help_text.append("Available Commands:\n", style="bold")
    help_text.append("  list-users          ", style="green")
    help_text.append("List all users with pagination\n")
    help_text.append("    --page N          ", style="dim")
    help_text.append("Show specific page (default: 1)\n", style="dim")
    help_text.append("    --per-page N      ", style="dim")
    help_text.append("Items per page (default: 20)\n\n", style="dim")
    help_text.append("  delete-channel <id> ", style="green")
    help_text.append("Delete a channel by ID\n\n")
    help_text.append("  delete-user <name>  ", style="green")
    help_text.append("Delete a user by username\n\n")
    help_text.append("  help                ", style="green")
    help_text.append("Show this help message\n\n")
    help_text.append("Examples:\n", style="bold")
    help_text.append("  docker compose run --rm keel python cli.py list-users\n", style="dim")
    help_text.append("  docker compose run --rm keel python cli.py list-users --page 2\n", style="dim")
    help_text.append("  docker compose run --rm keel python cli.py delete-channel ch_abc123\n", style="dim")
    help_text.append("  docker compose run --rm keel python cli.py delete-user john_doe\n", style="dim")
    console.print(Panel(help_text, title="Help", border_style="cyan", box=box.ROUNDED))

def main():
    if len(sys.argv)<2:
        show_help()
        sys.exit(0)
    parser=argparse.ArgumentParser(description="Keel CLI - User & Channel Management", add_help=False)
    parser.add_argument("command", nargs="?", help="Command to execute")
    parser.add_argument("argument", nargs="?", help="Command argument")
    parser.add_argument("--page", type=int, default=1, help="Page number for list-users")
    parser.add_argument("--per-page", type=int, default=20, help="Items per page for list-users")
    args=parser.parse_args()
    try:
        if args.command=="list-users":
            list_users(page=args.page, per_page=args.per_page)
        elif args.command=="delete-channel":
            if not args.argument:
                console.print("[red]Error: Channel ID required[/red]")
                console.print("Usage: delete-channel <channel_id>")
                sys.exit(1)
            delete_channel(args.argument)
        elif args.command=="delete-user":
            if not args.argument:
                console.print("[red]Error: Username required[/red]")
                console.print("Usage: delete-user <username>")
                sys.exit(1)
            delete_user(args.argument)
        elif args.command=="help":
            show_help()
        else:
            console.print(f"[red]Unknown command: {args.command}[/red]")
            show_help()
            sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user.[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

if __name__=="__main__":
    main()
