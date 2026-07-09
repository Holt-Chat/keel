import tomllib
import os
import sys
import logging
import nanoid
from threading import Event

def generate(size=20): return nanoid.generate(size=size)

stopping=Event()

_data_dir=sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(_data_dir, "version.toml"), "rb") as f:
    version_data=tomllib.load(f)
version=version_data["version"]
db_version=version_data["db"]

if not os.path.isfile("config.toml") or os.path.getsize("config.toml")==0:
    with open(os.path.join(_data_dir, "default_config.toml")) as fc:
        with open("config.toml", "w") as f:
            f.write("".join(fc.readlines()[2:]).replace("$URI_PREFIX", generate()))
    print("Wrote config.toml")
    sys.exit(1)
with open("config.toml", "rb") as f:
    config=tomllib.load(f)

if config["version"]<version_data["config"]:
    print("Your config.toml version doesn't match, please remove it and run the program again to create a new one")
    sys.exit(1)

dev_mode="--dev" in sys.argv or config["server"]["dev"]

logger=logging.getLogger("keel")

def setup_logging():
    level=logging.DEBUG if dev_mode else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

_log_levels={"INFO": logging.INFO, "DEV MODE INFO": logging.INFO, "LOG": logging.INFO, "DEV": logging.DEBUG, "WARNING": logging.WARNING, "ERROR": logging.ERROR}

def colored_log(color, tag, text): logger.log(_log_levels.get(tag, logging.INFO), f"{color}[{tag}]{RESET} {text}")
