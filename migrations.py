import os
import glob
from db import SQLite
from utils import colored_log, BLUE, RED

def run_migrations():
    with SQLite() as db:
        current_version=db.execute_raw_sql("PRAGMA user_version;")[0]["user_version"]
        if current_version==0: return current_version
        migration_files=sorted(glob.glob("migrations/*.sql"), key=lambda x: int(os.path.basename(x).split(".")[0]))
        if not migration_files: return current_version
        latest_migration=int(os.path.basename(migration_files[-1]).split(".")[0])
        if current_version>=latest_migration: return current_version
        colored_log(BLUE, "INFO", f"Running migrations from version {current_version} to {latest_migration}")
        for migration_file in migration_files:
            migration_version=int(os.path.basename(migration_file).split(".")[0])
            if migration_version>current_version:
                colored_log(BLUE, "INFO", f"Running migration {migration_version}")
                with open(migration_file, "r") as f:
                    migration_sql=f.read()
                try:
                    db.execute_script(migration_sql)
                    db.execute_raw_sql(f"PRAGMA user_version={migration_version}")
                    colored_log(BLUE, "INFO", f"Migration {migration_version} completed successfully")
                except Exception as e:
                    colored_log(RED, "ERROR", f"Migration {migration_version} failed: {e}")
                    raise
        colored_log(BLUE, "INFO", f"All migrations completed, database version is now {latest_migration}")
        return latest_migration