import argparse
import ctypes
import json
import os
import re
import sys
import time
import tomllib
import traceback
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen
from journal_reader import JournalReader, read_complete_line, commander_from_line, find_mission_accepts
from terminal_output import StatusLogger
from notifications import NotificationDispatcher, DeliveryError, LiveUpdate, check_response, telegram_send
from outbox import Outbox, profile_directory_name
from missions import MissionLedger
from ship_pools import load_ship_pools

try:
    from discord_webhook import DiscordWebhook
    discord_available = True
except ImportError:
    discord_available = False

def fallover(message):
    print(message)
    if sys.argv[0].count("\\") > 1:
        input("Press ENTER to exit")
    sys.exit()

# Internals
DEBUG_MODE = False
DISCORD_TEST = False
VERSION = 261017
DEFAULT_REPO = "sidonsoft/ED-AFK-Monitor"
DUPE_MAX = 5
FUEL_LOW = 0.2		# 20%
FUEL_CRIT = 0.1		# 10%
UNKNOWN = "[Unknown]"
REG_JOURNAL = r"^Journal\.\d{4}-\d{2}-\d{2}T\d{6}\.\d{2}\.log$"
REG_WEBHOOK = r"^https:\/\/(?:canary\.|ptb\.)?discord(?:app)?\.com\/api\/webhooks\/\d+\/[A-z0-9_-]+$"
WEBHOOK_AVATAR = "https://cdn.discordapp.com/attachments/1339930614064877570/1354083225923883038/t10.png"

# Game stuff
DEFAULT_SHIPS_EASY = ["adder", "asp", "asp_scout", "cobramkiii", "cobramkiv", "cobramkv", "diamondback", "diamondbackxl", "eagle", "empire_courier", "empire_eagle", "krait_light", "sidewinder", "viper", "viper_mkiv"]
DEFAULT_SHIPS_HARD = ["anaconda", "empire_trader", "federation_dropship", "federation_dropship_mkii", "federation_gunship", "ferdelance", "krait_mkii", "python", "python_nx", "type9_military", "typex", "typex_2", "typex_3", "vulture"]
BAIT_MESSAGES = ["$Pirate_ThreatTooHigh", "$Pirate_NotEnoughCargo", "$Pirate_OnNoCargoFound"]
COMBAT_RANKS = ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert", "Master", "Dangerous", "Deadly", "Elite", "Elite I", "Elite II", "Elite III", "Elite IV", "Elite V"]

# Config defaults
DEFAULTS_SETTINGS = {"JournalFolder": "", "UseUTC": False, "LiveStatus": True, "DynamicTitle": True, "WarnKillRate": 20, "WarnNoKills": 20, "PirateNames": False, "BountyFaction": False, "BountyValue": False, "ExtendedStats": False, "MinScanLevel": 1}
DEFAULTS_EXTRA = {"RecentFiles": 10, "TruncatePirate": 25, "TruncateFaction": 30, "WarnNoKillsInitial": 5, "WarnKillRateDelay": 5, "WarnCooldown": 30, "RecentIntervals": [15, 30, 60], "WinTitleCmdrName": False, "StatusPadChar": " ", "StatusColBG": "5;236", "StatusColFG": "5;254", "StatusSafeMargin": 2, "Repo": "sidonsoft/ED-AFK-Monitor", "ShipsFile": "ships.json", "WarnMissionExpiry": 15}
DEFAULTS_DISCORD = {"WebhookURL": "", "WebhookLiveURL": "", "UserID": 0, "PrependCmdrName": False, "ForumChannel": False, "ThreadCmdrNames": False, "Timestamp": True, "Identity": True}
DEFAULTS_TELEGRAM = {"BotToken": "", "ChatID": 0}
DEFAULTS_LOG_LEVELS = {"ScanIncoming": 1, "ScanEasy": 1, "ScanHard": 2, "KillEasy": 2, "KillHard": 2, "FighterHull": 2, "FighterDown": 3, "ShipShields": 3, "ShipHull": 3, "Died": 3, "CargoLost": 3, "BaitValueLow": 2, "SecurityScan": 2, "SecurityAttack": 3, "FuelReport": 1, "FuelLow": 2, "FuelCritical": 3, "Missions": 2, "MissionsAll": 3, "Merits": 0, "NoKills": 3, "KillRate": 3, "SummaryKills": 2, "SummaryFaction": 0, "SummaryScans": 0, "SummaryBounties": 2, "SummaryMerits": 2}

class Col:
    CYAN = "\x1b[96m"
    YELL = "\x1b[93m"
    EASY = "\x1b[38;5;157m"
    HARD = "\x1b[38;5;217m"
    WARN = "\x1b[38;5;215m"
    BAD = "\x1b[38;5;15m\x1b[48;5;1m"
    GOOD = "\x1b[38;5;15m\x1b[48;5;2m"
    WHITE = "\x1b[97m"
    END = "\x1b[0m"

WARNING = f"{Col.WARN}Warning:{Col.END}"

# Command line overrides
parser = argparse.ArgumentParser(
    prog="ED AFK Monitor",
    description="Live monitoring of Elite Dangerous AFK sessions to terminal, Discord and Telegram")
parser.add_argument("-p", "--profile", help="Load a specific profile for config settings")
parser.add_argument("-j", "--journal", help="Override for path to journal folder")
parser.add_argument("-w", "--webhook", help="Override for Discord logging webhook URL")
parser.add_argument("-wl", "--webhooklive", help="Override for Discord live webhook URL")
parser.add_argument("-r", "--resetsession", action="store_true", default=None, help="Reset session stats after preloading")
parser.add_argument("-t", "--test", action="store_true", default=None, help="Preview all notifications in the terminal without sending")
parser.add_argument("-d", "--debug", action="store_true", default=None, help="Print information for debugging")
parser.add_argument("--telegram-token", help="Override Telegram token (prefer ED_AFK_TELEGRAM_TOKEN environment variable)")
parser.add_argument("--telegram-chat", type=int, help="Override Telegram chat ID")
parser.add_argument("--no-follow", action="store_true", help="Keep monitoring only the selected journal")
parser.add_argument("--missions", action="store_true", help="Print the saved massacre-mission ledger and exit")
parser.add_argument("--json", action="store_true", help="With --missions, print machine-readable JSON")
parser.add_argument("--import-missions", metavar="file", help="Import a mission list (CSV or text, '-' for stdin) and exit")
parser.add_argument("--plain", action="store_true", help="Use plain text without colours, emojis or a live status line")
file_group = parser.add_mutually_exclusive_group()
file_group.add_argument("-s", "--setfile", help="Set specific journal file to use")
file_group.add_argument("-f", "--fileselect", action="store_true", default=None, help="Show list of recent journals to chose from")
args = parser.parse_args()

msg = StatusLogger(plain=args.plain)

# The update check and banner run after config is loaded (they honour the 'Repo' setting).

# Config loading and first run setup
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    config_file_path = Path(__file__).parents[1]
else:
    config_file_path = Path(__file__).parent


def ledger_paths(directory):
    return sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else []


def show_mission_ledgers(directory, as_json=False):
    paths = ledger_paths(directory)
    if as_json:
        data = []
        for path in paths:
            payload = MissionLedger(path).export()
            payload["file"] = path.name
            data.append(payload)
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return
    if not paths:
        msg.log("No mission ledger found. Start the monitor with massacre missions active to record them.")
        return
    for path in paths:
        ledger = MissionLedger(path)
        for warning in ledger.warnings:
            msg.log(warning)
        msg.log("")
        msg.log(ledger.report_text(include_missions=True, title=True))


def import_missions(directory, source, profile=None):
    paths = ledger_paths(directory)
    if profile:
        target = Path(directory) / f"{profile_directory_name(profile)}.json"
    elif len(paths) == 1:
        target = paths[0]
    elif not paths:
        target = Path(directory) / f"{profile_directory_name(None)}.json"
    else:
        msg.log("Multiple mission ledgers found; choose one with -p <profile>:")
        for existing in paths:
            msg.log(f"  {existing.stem}")
        return 2
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            text = Path(source).read_text(encoding="utf-8")
    except OSError as error:
        msg.log(f"Could not read '{source}': {error}")
        return 1
    ledger = MissionLedger(target)
    if profile and not ledger.profile:
        ledger.profile = profile
    for warning in ledger.warnings:
        msg.log(warning)
    added, replaced, errors = ledger.import_text(text)
    if errors:
        msg.log("Mission import failed:")
        for error in errors:
            msg.log(f"  {error}")
        return 1
    msg.log(f"Imported {added} mission(s) ({replaced} replaced)")
    msg.log("")
    msg.log(ledger.report_text(include_missions=True, title=True))
    return 0


if args.import_missions:
    sys.exit(import_missions(config_file_path / "mission_ledger",
                             args.import_missions, args.profile))

if args.missions:
    show_mission_ledgers(config_file_path / "mission_ledger", as_json=args.json)
    sys.exit()

config_file = Path(config_file_path / "afk_monitor.toml")
config_ready = False

while not config_ready:
    if config_file.is_file():
        with open(config_file, mode="rb") as f:
            try:
                config = tomllib.load(f)
                config_ready = True
            except tomllib.TOMLDecodeError as e:
                msg.log(f"Config decode error: {e}\n\nPlease check 'afk_monitor.toml' for errors and try again.")
    else:
        config_example = Path(config_file_path / "afk_monitor.example.toml")
        if config_example.is_file():
            try:
                import platform
                import shutil
                import subprocess
                shutil.copyfile(config_example, config_file)
                msg.log("Example config file copied to 'afk_monitor.toml'.\n\nEdit this file to configure settings (optional, but recommended) and continue when ready.")
                if platform.system() == "Windows":
                    os.startfile(config_file)
                elif platform.system() == "Linux":
                    subprocess.call(["xdg-open", config_file])
            except Exception as e:
                msg.log(f"Error copying example config: {e}\n\nPlease copy and rename 'afk_monitor.example.toml' to 'afk_monitor.toml' and try again.")
        else:
            fallover("Error: Example config file 'afk_monitor.example.toml' not found.\n\nPlease reinstall AFK Monitor and try again.")
    
    if not config_ready:
        response = input("\nPress ENTER to load config, or type anything then ENTER to quit:\n")
        if response != "":
            msg.log("\nQuitting...")
            sys.exit()

# Get settings from config
def getconfig(category: str, defaults: dict, warn_missing = True) -> dict:
    settings = {}
    for setting in defaults:
        this_setting = None
        # Check if setting exists in custom profile if one is set
        if profile and config.get(profile, {}).get(category, {}).get(setting) is not None:
            this_setting = config.get(profile, {}).get(category, {}).get(setting)
        # Check if setting exists under regular settings
        elif config.get(category, {}).get(setting) is not None:
            this_setting = config.get(category, {}).get(setting)
        # Otherwise use provided default
        else:
            this_setting = defaults[setting]
            if warn_missing:
                msg.log(f"{WARNING} Config '{category}' -> '{setting}' not found (using default: '{defaults[setting]}')")
        
        # Check setting matches type provided in defaults
        if type(this_setting) is not type(defaults[setting]):
            msg.log(f"{WARNING} Config '{category}' -> '{setting}' expected type {type(defaults[setting]).__name__} but got {type(this_setting).__name__} (using default: {defaults[setting]})")
            this_setting =  defaults[setting]
            
        settings[setting] = this_setting
    return(settings)

# Get settings from arguments
profile = args.profile if args.profile is not None else None
setting_fileselect = args.fileselect if args.fileselect is not None else False
setting_journal_dir = args.journal if args.journal is not None else getconfig("Settings", {"JournalFolder": ""}, False)["JournalFolder"]
setting_recent_files = getconfig("Settings", {"RecentFiles": DEFAULTS_EXTRA["RecentFiles"]}, False)["RecentFiles"]
setting_journal_file = args.setfile if args.setfile is not None else None
discord_test = args.test if args.test is not None else DISCORD_TEST
debug_mode = args.debug if args.debug is not None else DEBUG_MODE

def debug(message):
    if debug_mode:
        msg.log(f"{Col.WHITE}[Debug]{Col.END} {message} [{datetime.strftime(datetime.now(), '%H:%M:%S')}]")

debug(f"Profile argument: {args.profile}")

class Stats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.scansinrecents = []
        self.scansoutrecents = []
        self.lastkillutc = 0
        self.lastkillmono = 0
        self.killstime = 0
        self.kill_times = []
        self.scansin = 0
        self.scanstime = 0
        self.scan_times = []
        self.lastscanutc = 0
        self.lastscanmono = 0
        self.kills = 0
        self.bounties = 0
        self.factions = {}
        self.merits = 0
        self.lastsecurity = ""
        self.baitfails = 0
        self.fuellasttime = 0
        self.fuellastremain = 0
        self.meritstoreport = 0

class Tracking:
    def __init__(self):
        self.deploytime = None
        self.totaltime = 0
        self.warnednokills = None
        self.warnedkillrate = None
        self.fuelcapacity = 64
        self.killtype = "bounties"
        self.fighterhull = 0
        self.logged = 0
        self.lines = 0
        self.missions = False
        self.lasteventname = None
        self.thiseventtime = None
        self.dupeevent = ""
        self.duperepeats = 1
        self.dupewarn = False
        self.warned_missions = set()
        self.preloading = True
        self.cmdrname = None
        self.cmdrship = None
        self.cmdrcombatrank = None
        self.cmdrcombatprogress = None
        self.cmdrgamemode = None
        self.cmdrlocation = None
        self.lastcheck = None
        self.live_update = None
        self.live_reset = True
    
    def sessionstart(self, reset=False, delta=0):
        if not self.deploytime or reset:
            if not delta:
                self.deploytime = self.thiseventtime
            else:
                self.deploytime = self.thiseventtime - timedelta(seconds=delta)
            session.reset()
            debug(f"Session started: {self.deploytime}")
            session.kill_times.append(self.deploytime)
            session.scan_times.append(self.deploytime)
            self.warnednokills = None
            self.warnedkillrate = None
            self.lastcheck = time.monotonic()
            self.live_update = None
            update_status()

    def sessionend(self):
        if self.deploytime:
            self.totaltime += (self.thiseventtime - self.deploytime).total_seconds()
            debug(f"Session ended: {self.thiseventtime} ({time_format((self.thiseventtime-self.deploytime).total_seconds())}) [Total: {time_format(self.totaltime)}]")
            self.deploytime = None
            self.live_reset = True
            update_status(True)

session = Stats()
total = Stats()
track = Tracking()

# Set journal directory
if not setting_journal_dir:
    journal_dir = Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"
else:
    journal_dir = Path(setting_journal_dir)
if setting_journal_file and Path(setting_journal_file).expanduser().is_absolute():
    journal_dir = Path(setting_journal_file).expanduser().parent
if not journal_dir.is_dir():
    fallover(f"Directory {journal_dir} not found")

msg.log(f"{Col.YELL}Journal folder:{Col.END} {journal_dir}")

# Set journal file
if not setting_journal_file:
    journals = []
    journal_file = None

    # Get recent journals, newest first
    for entry in sorted(journal_dir.iterdir(), reverse=True):
        if entry.is_file() and bool(re.search(REG_JOURNAL, entry.name)):
            if not setting_fileselect:
                # Just the latest journal
                journal_file = entry.name
                break
            else:
                # Build list of recent journals
                journals.append(entry.name)
                if len(journals) == setting_recent_files:
                    break
    
    # Exit if no journals were found
    if not journal_file and len(journals) == 0:
        fallover("Journal folder does not contain any valid journal files")
    
    # Journal selector
    if setting_fileselect:
        msg.log("\nLatest journals:")

        # Get commander name from each journal and output list
        commanders = []
        for i, filename in enumerate(journals, start=1):
            commander = None
            with open(Path(journal_dir / filename), mode="r", encoding="utf-8", errors="replace") as file:
                while line := read_complete_line(file):
                    commander = commander_from_line(line)
                    if commander:
                        break

            num = f"{i:>{len(str(len(journals)))}}"
            msg.log(f"{num} | {filename} | CMDR {commander if commander else UNKNOWN}")
            commanders.append(commander)

        # Prompt for journal choice
        msg.log("\nInput journal number to load")
        selection = input("(ENTER for latest or any other input to quit)\n")
        if selection:
            try:
                selection = int(selection)
                if 1 <= selection <= len(journals):
                    journal_file = journals[selection-1]
                    track.cmdrname = commanders[selection-1]
                else:
                    fallover("Invalid number, exiting...")
            except ValueError:
                fallover("Exiting...")
        else:
            journal_file = journals[0]
            track.cmdrname = commanders[0]
elif setting_journal_file:
    selected = Path(setting_journal_file).expanduser()
    selected = selected if selected.is_absolute() else journal_dir / selected
    if not re.fullmatch(REG_JOURNAL, selected.name) or not selected.is_file():
        fallover(f"Journal file '{setting_journal_file}' invalid or not found")
    journal_dir = selected.parent
    journal_file = selected.name

msg.log(f"{Col.YELL}Journal file:{Col.END} {journal_file}")

# Wait for complete Commander events, following restarts even before game load.
if not track.cmdrname:
    try:
        with JournalReader(journal_dir / journal_file,
                           follow=not (args.no_follow or args.setfile or args.fileselect)) as file:
            waiting = False
            while not track.cmdrname:
                line = file.readline()
                journal_file = file.path.name
                if line:
                    track.cmdrname = commander_from_line(line)
                else:
                    if not waiting:
                        msg.log("Waiting for game load... (Press Ctrl+C to stop)")
                        waiting = True
                    time.sleep(1)
    except KeyboardInterrupt:
        fallover("Quitting...")

msg.log(f"{Col.YELL}Commander name:{Col.END} {track.cmdrname}")

# Check for a config profile if one is set
config_info = ""
if not args.profile:
    profile = track.cmdrname
    if profile in config:
        config_info = " (auto)"
if profile and profile not in config:
    debug(f"No config settings for '{profile}' found")
    profile = None

msg.log(f"{Col.YELL}Config profile:{Col.END} {profile if profile else 'Default'}{config_info}")
if profile:
    debug(f"Using profile: {profile}")

mission_ledger = MissionLedger(
    config_file_path / "mission_ledger" / f"{profile_directory_name(profile)}.json",
    commander=track.cmdrname or "", profile=profile or "",
    details_lookup=lambda ids: find_mission_accepts(journal_dir, ids))
for warning in mission_ledger.warnings:
    msg.log(warning)

# Get settings from config
conf_settings = getconfig("Settings", DEFAULTS_SETTINGS)
conf_settings.update(getconfig("Settings", DEFAULTS_EXTRA, False))
conf_discord = getconfig("Discord", DEFAULTS_DISCORD)
conf_discord.update(getconfig("Discord", {"WebhookAvatarURL": WEBHOOK_AVATAR}, False))
conf_log_levels = getconfig("LogLevels", DEFAULTS_LOG_LEVELS)
conf_log_levels.update(getconfig("LogLevels", {"SummaryMissions": 2, "MissionExpiry": 3}, False))
conf_telegram = getconfig("Telegram", DEFAULTS_TELEGRAM, False)
telegram_token = args.telegram_token if args.telegram_token is not None else os.environ.get("ED_AFK_TELEGRAM_TOKEN", conf_telegram["BotToken"])
telegram_chat = args.telegram_chat if args.telegram_chat is not None else conf_telegram["ChatID"]
telegram_enabled = bool(telegram_token and telegram_chat)
follow_journals = not (args.no_follow or args.setfile or args.fileselect)
if bool(telegram_token) != bool(telegram_chat):
    msg.log("Telegram: both BotToken and ChatID are required; Telegram disabled")

# Update check (offline mission commands exit before this point)
github_repo = conf_settings["Repo"] or DEFAULT_REPO
latest_version = 0
try:
    with urlopen(f"https://api.github.com/repos/{github_repo}/releases/latest", timeout=1) as response:
        if response.status == 200:
            release_data = json.loads(response.read())
            latest_version = int(release_data["tag_name"][1:])
except Exception:
    pass

title = f"ED AFK Monitor v{VERSION} by CMDR PSIPAB"
msg.log(f"{Col.CYAN}{'='*len(title)}")
msg.log(f"{title}")
msg.log(f"{'='*len(title)}{Col.END}\n")
if VERSION < latest_version:
    msg.log(f"{Col.YELL}Update v{latest_version} is available!{Col.END}\n{Col.WHITE}Download:{Col.END} https://github.com/{github_repo}/releases\n")

# Ship pools can be replaced with a data file so game updates do not need code edits
ships_path = Path(conf_settings["ShipsFile"])
if not ships_path.is_absolute():
    ships_path = config_file_path / ships_path
SHIPS_EASY, SHIPS_HARD, ship_warnings = load_ship_pools(ships_path, DEFAULT_SHIPS_EASY, DEFAULT_SHIPS_HARD)
for warning in ship_warnings:
    msg.log(f"{WARNING} {warning}")

# Configure status line
status_col = f"\x1b[48;{conf_settings['StatusColBG']}m" if conf_settings["StatusColBG"] else ""
status_col += f"\x1b[38;{conf_settings['StatusColFG']}m" if conf_settings["StatusColFG"] else ""
msg.endcol = msg.ANSI_COL_END if status_col else ""
msg.safe = conf_settings["StatusSafeMargin"]
msg.pad = conf_settings["StatusPadChar"]

debug(f"Settings: {conf_settings}")
debug(f"Discord logging configured: {bool(conf_discord['WebhookURL'])}; live configured: {bool(conf_discord['WebhookLiveURL'])}")
debug(f"Log levels: {conf_log_levels}")

msg.log("\nStarting... (Press Ctrl+C to stop)\n")

# Discord webhooks
discord_webhook_log = args.webhook if args.webhook is not None else conf_discord["WebhookURL"]
discord_webhook_live = args.webhooklive if args.webhooklive is not None else conf_discord["WebhookLiveURL"]
webhook_log = None if not discord_test else True
webhook_live = None if not discord_test else True

if discord_available:
    # Logging webhook
    if discord_webhook_log and re.search(REG_WEBHOOK, discord_webhook_log):
        webhook_log = DiscordWebhook(url=discord_webhook_log, timeout=5)
        if conf_discord["Identity"]:
            webhook_log.username = "ED AFK Monitor"
            webhook_log.avatar_url = conf_discord["WebhookAvatarURL"]
        if conf_discord["ForumChannel"]:
            journal_start = datetime.fromisoformat(journal_file[8:-7])
            journal_start = datetime.strftime(journal_start, "%Y-%m-%d %H:%M:%S")
            if conf_discord["ThreadCmdrNames"]:
                webhook_log.thread_name = f"{track.cmdrname} {journal_start}"
            else:
                webhook_log.thread_name = journal_start
            #debug(f"webhook_log.thread_name: {webhook_log.thread_name}")
    elif discord_webhook_log:
        msg.log(f"{Col.WARN}Warning:{Col.END} Discord (logging) webhook invalid URL; logging to terminal only\n")

    # Live stats webhook
    if discord_webhook_live and re.search(REG_WEBHOOK, discord_webhook_live):
        webhook_live = DiscordWebhook(url=discord_webhook_live, timeout=5)
        if conf_discord["Identity"]:
            webhook_live.username = "ED AFK Monitor Live"
            webhook_live.avatar_url = conf_discord["WebhookAvatarURL"]
    elif discord_webhook_live:
        msg.log(f"{Col.WARN}Warning:{Col.END} Discord (live) webhook invalid URL; live stats for Discord disabled\n")
elif discord_webhook_log or discord_webhook_live:
    msg.log(f"{Col.WARN}Warning:{Col.END} discord-webhook module unavailable; operating with terminal output only\n")

# These transports run only on their own background workers.
def discordsend(message=""):
    webhook_log.content = message
    try:
        response = webhook_log.execute()
    except Exception:
        raise DeliveryError("connection failed or timed out", retryable=True) from None
    if response is None:
        raise DeliveryError("no response", retryable=True)
    check_response(response.status_code, response.headers.get("Retry-After") if hasattr(response, "headers") else None)
    if conf_discord["ForumChannel"] and webhook_log.thread_name and not webhook_log.thread_id:
        webhook_log.thread_name = None
        webhook_log.thread_id = webhook_log.id


live_delivery_generation = None
live_generation = 0


def discord_live_send(update):
    global live_delivery_generation
    webhook_live.content = update.text
    use_edit = (update.generation == live_delivery_generation
                and bool(getattr(webhook_live, "id", None)))
    try:
        response = webhook_live.edit() if use_edit else webhook_live.execute()
        if use_edit and getattr(response, "status_code", None) == 404:
            # The live message was deleted; post a new one instead of failing forever.
            webhook_live.id = None
            live_delivery_generation = None
            response = webhook_live.execute()
    except Exception:
        raise DeliveryError("connection failed or timed out", retryable=True) from None
    if response is None:
        raise DeliveryError("no response", retryable=True)
    check_response(response.status_code, response.headers.get("Retry-After") if hasattr(response, "headers") else None)
    live_delivery_generation = update.generation


senders = {}
if webhook_log:
    senders["discord"] = discordsend
if webhook_live:
    senders["discord_live"] = discord_live_send
if telegram_enabled:
    senders["telegram"] = lambda message: telegram_send(telegram_token, telegram_chat, message)
notifications_outbox = None
if senders and not discord_test:
    notifications_outbox = Outbox(
        config_file_path / "pending_notifications" / profile_directory_name(profile),
        [name for name in senders if name != "discord_live"])
notifications = NotificationDispatcher(senders, preview=discord_test, outbox=notifications_outbox,
                                       max_lengths={"discord": 2000, "telegram": 4096})


def notification_reports():
    for report in notifications.drain_reports():
        msg.log(report)


def sendmessage(message="", telegram_message=None):
    notifications.submit("discord", message)
    if telegram_enabled:
        notifications.submit("telegram", message if telegram_message is None else telegram_message)
    notification_reports()


def discord_live_update(message=""):
    global live_generation
    if track.live_reset:
        live_generation += 1
        track.live_reset = False
    cmdr_name = "" if not conf_discord["PrependCmdrName"] else f"**{track.cmdrname}** "
    notifications.submit("discord_live", LiveUpdate(f"{cmdr_name}{message}", live_generation))
    notification_reports()

# Log events
def logevent(msg_term, msg_discord=None, emoji=None, timestamp=None, loglevel=2, event=None, msg_telegram=None):
    emoji = f"{emoji} " if emoji else ""
    loglevel = int(loglevel)
    if track.preloading and not discord_test:
        loglevel = 1 if loglevel > 0 else 0
    if timestamp:
        logtime = timestamp if conf_settings["UseUTC"] else timestamp.astimezone()
    else:
        logtime = datetime.now(timezone.utc) if conf_settings["UseUTC"] else datetime.now().astimezone()
    logtime = datetime.strftime(logtime, "%H:%M:%S")
    track.logged +=1
    
    # Terminal
    if loglevel > 0 and not discord_test:
        msg.log(f"[{logtime}]{emoji}{msg_term}")
    
    # Remote notifications
    if (webhook_log or telegram_enabled) and loglevel > 1:
        if event is not None and track.dupeevent == event:
            track.duperepeats += 1
        else:
            track.duperepeats = 1
            track.dupewarn = False
        track.dupeevent = event
        discord_message = msg_discord if msg_discord else f"**{msg_term}**"
        ping = f" <@{conf_discord['UserID']}>" if loglevel > 2 and track.duperepeats == 1 else ""
        logtime = f" {{{logtime}}}" if conf_discord["Timestamp"] else ""
        cmdrname = "" if not conf_discord["PrependCmdrName"] else f"[{track.cmdrname}] "
        telegram_message = msg_term if msg_telegram is None else msg_telegram
        if track.duperepeats <= DUPE_MAX:
            sendmessage(f"{cmdrname}{emoji}{discord_message}{logtime}{ping}",
                        telegram_message=re.sub(r"\x1b\[[0-9;]*m", "", f"{track.cmdrname}: {emoji}{telegram_message}{logtime}"))
        elif not track.dupewarn:
            sendmessage(f"{cmdrname}⏸️ **Suppressing further duplicate messages**{logtime}",
                        telegram_message=f"⏸️ Suppressing further duplicate messages{logtime}")
            track.dupewarn = True

# Calculate somethings per hour
def per_hour(seconds=0, precision=None):
    if seconds > 0:
        return round(3600 / seconds, precision)
    else:
        return 0

def recent_rates(timestamps: list, checktime: datetime, intervals: list, precision=None):
    """Calculates the number of events per hour of a given time range"""
    
    if checktime is None:
        checktime = datetime.now(timezone.utc)
    
    try:
        rates_out = {}
        if len(timestamps) > 1:
            duration = checktime - timestamps[0]
            for i in intervals:
                if duration > timedelta(minutes=i):
                    delta = timedelta(minutes=i)
                    start = checktime - delta
                    time_since = (checktime - timestamps[-1]).total_seconds()
                    index_start = bisect_left(timestamps, start) - 1
                    index_end = bisect_left(timestamps, checktime)
                    num_events = max(0, index_end - index_start)
                    if num_events > 1:
                        per_hour = (3600 / (delta.total_seconds() + time_since)) * (num_events -1)
                        rates_out[str(i)] = str(round(per_hour, precision))
                    else:
                        rates_out[str(i)] = "0"
    except Exception as e:
        msg.log(f"Error: {e}")
    
    # Format output
    if rates_out:
        keys = "/".join(rates_out.keys())
        vals = "/".join(rates_out.values())
        return f"{keys}m: {vals}h"
    else:
        return None

# Shorten a string
def truncate(input: str, chars: int) -> str:
    if len(input) <= chars+1:
        return input
    else:
        return f"{input[:chars].rstrip()}…"

# Process incoming journal entries
def processevent(line):
    try:
        j = json.loads(line)
    except ValueError:
        msg.log(f"{Col.WHITE}Warning:{Col.END} Journal parsing error, skipping line")
        return

    try:
        logtime = datetime.fromisoformat(j["timestamp"]) if "timestamp" in j else None
        track.thiseventtime = logtime
        mission_ledger.observe_location(j)
        match j["event"]:
            case "ReceiveText" if j["Channel"] == "npc":
                if "$Pirate_OnStartScanCargo" in j["Message"]:
                    piratename = j["From_Localised"] if "From_Localised" in j else UNKNOWN
                    if piratename not in session.scansinrecents:
                        session.scansin += 1
                        total.scansin += 1
                        scansin = f" (x{session.scansin})" if conf_settings["ExtendedStats"] else ""
                        pirate = f" [{piratename}]" if conf_settings["PirateNames"] else ""
                        
                        if len(session.scansinrecents) == 5:
                            session.scansinrecents.pop(0)
                        session.scansinrecents.append(piratename)
                        
                        thisscan = logtime
                        if session.lastscanutc:
                            seconds = int((thisscan-session.lastscanutc).total_seconds())
                            session.scanstime += seconds
                            total.scanstime += seconds
                        session.lastscanutc = logtime
                        session.scan_times.append(logtime)
                        if not track.preloading:
                            session.lastscanmono = time.monotonic()
                        
                        logevent(msg_term=f"Cargo scan{scansin}{pirate}",
                                    msg_discord=f"**Cargo scan{scansin}**{pirate}",
                                emoji="📦", timestamp=logtime, loglevel=conf_log_levels["ScanIncoming"])
                elif any(x in j["Message"] for x in BAIT_MESSAGES):
                    session.baitfails += 1
                    baitfails = f" (x{session.baitfails})" if conf_settings["ExtendedStats"] else ""
                    logevent(msg_term=f"{Col.WARN}Pirate didn\"t engage due to insufficient cargo value{baitfails}{Col.END}",
                            msg_discord=f"**Pirate didn\"t engage due to insufficient cargo value**{baitfails}",
                            emoji="🎣", timestamp=logtime, loglevel=conf_log_levels["BaitValueLow"], event="BaitValueLow")
                elif "Police_Attack" in j["Message"]:
                    logevent(msg_term=f"{Col.BAD}Under attack by security services!{Col.END}",
                            msg_discord="**Under attack by security services!**",
                            emoji="🚨", timestamp=logtime, loglevel=conf_log_levels["SecurityAttack"])
            case "ShipTargeted" if "Ship" in j:
                ship = j["Ship_Localised"] if "Ship_Localised" in j else j["Ship"].title()
                rank = "" if "PilotRank" not in j else f" ({j['PilotRank']})"
                # Security
                if ship != session.lastsecurity and "PilotName" in j and "$ShipName_Police" in j["PilotName"]:
                    session.lastsecurity = ship
                    logevent(msg_term=f"{Col.WARN}Scanned security{Col.END} ({ship})",
                            msg_discord=f"**Scanned security** ({ship})",
                            emoji="🚨", timestamp=logtime, loglevel=conf_log_levels["SecurityScan"])
                # Pirates etc.
                elif j["Ship"] in SHIPS_EASY or j["Ship"] in SHIPS_HARD:
                    if not track.deploytime:
                        track.sessionstart(delta=30)
                    piratename = j["PilotName_Localised"] if "PilotName_Localised" in j else UNKNOWN
                    check = piratename if conf_settings["MinScanLevel"] != 0 else ship
                    scanstage = j["ScanStage"] if "ScanStage" in j else 0
                    if scanstage >= conf_settings["MinScanLevel"] and check not in session.scansoutrecents:
                        if len(session.scansoutrecents) == 10:
                            session.scansoutrecents.pop(0)
                        session.scansoutrecents.append(check)
                        pirate = f" [{piratename}]" if conf_settings["PirateNames"] and piratename != UNKNOWN else ""
                        hard = ""
                        log = conf_log_levels["ScanEasy"]
                        if j["Ship"] in SHIPS_EASY:
                            col = Col.EASY
                        elif j["Ship"] in SHIPS_HARD:
                            col = Col.HARD
                            log = conf_log_levels["ScanHard"]
                            hard = " ☠️"
                        else:
                            col = Col.WHITE
                        logevent(msg_term=f"{col}Scan{Col.END}: {ship}{rank}{pirate}",
                                msg_discord=f"**{ship}**{hard}{rank}{pirate}",
                                emoji="🔎", timestamp=logtime, loglevel=log)
            case "Bounty" | "FactionKillBond":
                if not track.deploytime:
                    track.sessionstart(delta=60)
                
                if conf_settings["MinScanLevel"] == 0:
                    session.scansoutrecents.clear()
                session.kills +=1
                total.kills +=1
                thiskill = logtime
                killtime = ""
                track.lastcheck = time.monotonic()
                session.meritstoreport +=1
                
                if session.lastkillutc:
                    seconds = int((thiskill-session.lastkillutc).total_seconds())
                    killtime = f" (+{time_format(seconds)})"
                    session.killstime += seconds
                    total.killstime += seconds
                session.lastkillutc = logtime
                session.kill_times.append(logtime)
                if not track.preloading:
                    session.lastkillmono = time.monotonic()
                
                hard = ""
                log = conf_log_levels["KillEasy"]
                col = Col.WHITE
                if j["event"] == "Bounty":
                    if j["Target"] in SHIPS_EASY:
                        col = Col.EASY
                    elif j["Target"] in SHIPS_HARD:
                        col = Col.HARD
                        log = conf_log_levels["KillHard"]
                        hard = " ☠️"
                    
                    bountyvalue = j["Rewards"][0]["Reward"]
                    ship = j["Target_Localised"] if "Target_Localised" in j else j["Target"].title()
                else:
                    bountyvalue = j["Reward"]
                    ship = "Bond"
                    track.killtype = "bonds"

                piratename = f" [{truncate(j['PilotName_Localised'], conf_settings['TruncatePirate'])}]" if "PilotName_Localised" in j and conf_settings["PirateNames"] else ""
                session.bounties += bountyvalue
                total.bounties += bountyvalue
                kills_t = f" x{session.kills}" if conf_settings["ExtendedStats"] else ""
                kills_d = f"x{session.kills} " if conf_settings["ExtendedStats"] else ""
                bountyvalue = f" [{num_format(bountyvalue)} cr]" if conf_settings["BountyValue"] else ""
                victimfaction = j.get("VictimFaction_Localised") or j.get("VictimFaction") or UNKNOWN
                if j["event"] == "Bounty":
                    mission_ledger.kill(victimfaction, j.get("VictimFaction"))
                session.factions[victimfaction] = session.factions.get(victimfaction, 0) + 1
                total.factions[victimfaction] = total.factions.get(victimfaction, 0) + 1
                factioncount = f" x{session.factions[victimfaction]}" if conf_settings["ExtendedStats"] else ""
                bountyfaction = truncate(victimfaction, conf_settings["TruncateFaction"])
                bountyfaction = f" [{bountyfaction}{factioncount}]" if conf_settings["BountyFaction"] else ""
                logevent(msg_term=f"{col}Kill{Col.END}{kills_t}: {ship}{killtime}{piratename}{bountyvalue}{bountyfaction}",
                        msg_discord=f"{kills_d}**{ship}{hard}{killtime}**{piratename}{bountyvalue}{bountyfaction}",
                        emoji="💥", timestamp=logtime, loglevel=log)

                update_status()
                
                # Output stats every 10 kills
                if session.kills % 10 == 0:
                    summary(session, logtime=logtime)
            case "MissionRedirected" if "Mission_Massacre" in j["Name"]:
                mission = mission_ledger.redirected(j.get("MissionID"))
                done, mission_total = mission_ledger.progress()
                if mission_total and done == mission_total:
                    log = conf_log_levels["MissionsAll"]
                    type = "all missions!"
                else:
                    log = conf_log_levels["Missions"]
                    type = "a mission"
                detail = f" ({mission.target})" if mission and mission.target else ""
                logevent(msg_term=f"Completed kills for {type} ({done}/{mission_total}){detail}",
                        emoji="✅", timestamp=logtime, loglevel=log)
                track.live_update = None
                update_status()
            case "ReservoirReplenished":
                fuelremaining = round((j["FuelMain"] / track.fuelcapacity) * 100)
                if session.fuellasttime and track.deploytime and logtime > session.fuellasttime:
                    fuel_time = (logtime-session.fuellasttime).total_seconds()
                    fuel_hour = 3600 / fuel_time * (session.fuellastremain-j["FuelMain"])
                    if fuel_hour > 0:
                        fuel_time_remain = time_format(j["FuelMain"] / fuel_hour * 3600)
                        fuel_time_remain = f" (~{fuel_time_remain})"
                    else:
                        fuel_time_remain = ""
                    #debug(f"Fuel used since previous: {round(session.fuellastremain-j["FuelMain"],2)}t in {time_format(fuel_time)}")
                else:
                    fuel_time_remain = ""

                session.fuellasttime = logtime
                session.fuellastremain = j["FuelMain"]

                col = ""
                level = ":"
                fuel_loglevel = 0
                if j["FuelMain"] < track.fuelcapacity * FUEL_CRIT:
                    col = Col.BAD
                    fuel_loglevel = conf_log_levels["FuelCritical"]
                    level = " critical!"
                elif j["FuelMain"] < track.fuelcapacity * FUEL_LOW:
                    col = Col.WARN
                    fuel_loglevel = conf_log_levels["FuelLow"]
                    level = " low:"
                elif track.deploytime:
                    fuel_loglevel = conf_log_levels["FuelReport"]

                logevent(msg_term=f"{col}Fuel: {fuelremaining}% remaining{Col.END}{fuel_time_remain}",
                    msg_discord=f"**Fuel{level} {fuelremaining}% remaining**{fuel_time_remain}",
                    emoji="⛽", timestamp=logtime, loglevel=fuel_loglevel)
            case "FighterDestroyed" if track.lasteventname != "StartJump":
                logevent(msg_term=f"{Col.BAD}Fighter destroyed!{Col.END}",
                        msg_discord="**Fighter destroyed!**",
                        emoji="🕹️", timestamp=logtime, loglevel=conf_log_levels["FighterDown"])
            case "LaunchFighter" if not j["PlayerControlled"]:
                logevent(msg_term="Fighter launched",
                        emoji="🕹️", timestamp=logtime, loglevel=2)
            case "ShieldState":
                if j["ShieldsUp"]:
                    shields = "back up"
                    col = Col.GOOD
                else:
                    shields = "down!"
                    col = Col.BAD
                logevent(msg_term=f"{col}Ship shields {shields}{Col.END}",
                        msg_discord=f"**Ship shields {shields}**",
                        emoji="🛡️", timestamp=logtime, loglevel=conf_log_levels["ShipShields"])
            case "HullDamage":
                hullhealth = round(j["Health"] * 100)
                if j["Fighter"] and not j["PlayerPilot"] and track.fighterhull != j["Health"]:
                    track.fighterhull = j["Health"]
                    logevent(msg_term=f"{Col.WARN}Fighter hull damaged!{Col.END} (Integrity: {hullhealth}%)",
                        msg_discord=f"**Fighter hull damaged!** (Integrity: {hullhealth}%)",
                        emoji="🕹️", timestamp=logtime, loglevel=conf_log_levels["FighterHull"])
                elif j["PlayerPilot"] and not j["Fighter"]:
                    logevent(msg_term=f"{Col.BAD}Ship hull damaged!{Col.END} (Integrity: {hullhealth}%)",
                        msg_discord=f"**Ship hull damaged!** (Integrity: {hullhealth}%)",
                        emoji="🛠️", timestamp=logtime, loglevel=conf_log_levels["ShipHull"])
            case "Died":
                logevent(msg_term=f"{Col.BAD}Ship destroyed!{Col.END}",
                        msg_discord="**Ship destroyed!**",
                        emoji="💀", timestamp=logtime, loglevel=conf_log_levels["Died"])
            case "Music" if j["MusicTrack"] == "MainMenu":
                track.sessionend()
                logevent(msg_term="Exited to main menu",
                    emoji="🚪", timestamp=logtime, loglevel=2)
            case "Commander":
                if track.cmdrname and j["Name"] != track.cmdrname:
                    msg.log("Commander changed; restart the monitor to load the correct profile")
                    raise SystemExit()
                track.cmdrname = j["Name"]
            case "LoadGame":
                track.sessionend()
                session.reset()
                track.fighterhull = 0
                track.live_update = None
                track.missions = False
                track.warned_missions = set()
                # Duplicate suppression must not carry a previous session's counter over.
                track.dupeevent = ""
                track.duperepeats = 1
                track.dupewarn = False
                if "Ship_Localised" in j:
                    track.cmdrship = j["Ship_Localised"]
                elif "Ship" in j:
                    track.cmdrship = j["Ship"]

                if "GameMode" in j:
                    track.cmdrgamemode = "Private Group" if j["GameMode"] == "Group" else j["GameMode"]

                cmdrinfo =  f"{track.cmdrship} / {track.cmdrgamemode} / {track.cmdrcombatrank} +{track.cmdrcombatprogress}%"
                
                logevent(msg_term=f"CMDR {track.cmdrname} ({cmdrinfo})",
                            msg_discord=f"**CMDR {track.cmdrname}** ({cmdrinfo})",
                            emoji="🔄", timestamp=logtime, loglevel=2)
            case "Loadout":
                track.fuelcapacity = j["FuelCapacity"]["Main"] if j["FuelCapacity"]["Main"] >= 2 else 64
                #debug(f"Fuel capacity: {track.fuelcapacity}")
            case "SupercruiseDestinationDrop" if any(x in j["Type"] for x in ["$MULTIPLAYER", "$Warzone"]):
                track.sessionstart(True)
                type = j["Type_Localised"] if "Type_Localised" in j else UNKNOWN
                if "Resource Extraction Site" in type:
                    emoji = "🪐"
                else:
                    emoji = "⚔️"
                logevent(msg_term=f"Dropped at {type}",
                        emoji=emoji, timestamp=logtime, loglevel=2)
                debug(f"Deploy time by supercruise drop: {track.deploytime}")
            case "EjectCargo" if not j["Abandoned"] and j["Count"] == 1:
                name = j["Type_Localised"] if "Type_Localised" in j else j["Type"].title()
                logevent(msg_term=f"{Col.BAD}Cargo lost or jettisoned (possible theft){Col.END} ({name})",
                        msg_discord=f"**Cargo lost or jettisoned (possible theft)** ({name})",
                        emoji="🪓", timestamp=logtime, loglevel=conf_log_levels["CargoLost"], event="CargoLost")
            case "Rank":
                track.cmdrcombatrank = COMBAT_RANKS[j["Combat"]]
            case "Progress":
                track.cmdrcombatprogress = j["Combat"]
            case "Missions" if "Active" in j and not track.missions:
                mission_ledger.reconcile(j["Active"], logtime)
                track.missions = True
                track.live_update = None
                if mission_ledger.backfilled:
                    msg.log(f"Missions: recovered details for {mission_ledger.backfilled} mission(s)"
                            f" from earlier journals")
                logevent(msg_term=f"Missions loaded (active massacres: {mission_ledger.count()})",
                        emoji="🎯", timestamp=logtime, loglevel=conf_log_levels["Missions"])
            case "MissionAccepted" if "Mission_Massacre" in j["Name"]:
                mission = mission_ledger.accept(j)
                if mission:
                    detail = f": {mission.target} x{mission.kill_count}" if mission.known else ""
                    wing = " (wing)" if mission.wing else ""
                    logevent(msg_term=f"Accepted massacre mission{detail}{wing} (active: {mission_ledger.count()})",
                            emoji="🎯", timestamp=logtime, loglevel=conf_log_levels["Missions"])
                track.live_update = None
            case "MissionAbandoned" | "MissionCompleted" | "MissionFailed":
                mission = mission_ledger.close(j.get("MissionID"))
                if mission:
                    event = j["event"][7:].lower()
                    detail = f": {mission.target}" if mission.target else ""
                    logevent(msg_term=f"Massacre mission {event}{detail} (active: {mission_ledger.count()})",
                            emoji="🎯", timestamp=logtime, loglevel=conf_log_levels["Missions"])
                track.live_update = None
            case "PowerplayMerits":
                if session.meritstoreport > 0 and j["MeritsGained"] < 500:
                    session.merits += j["MeritsGained"]
                    total.merits += j["MeritsGained"]
                    logevent(msg_term=f"Merits: +{j['MeritsGained']} ({j['Power']})",
                                emoji="🎫", timestamp=logtime, loglevel=conf_log_levels["Merits"])
                    session.meritstoreport -= 1
            case "Location":
                #track.cmdrlocation = j["StarSystem"]
                if j["BodyType"] == "PlanetaryRing":
                    track.sessionstart()
                    #debug(f"Deploy time by location (planetary ring) {track.deploytime}")
            case "ShipyardSwap":
                track.cmdrship = j["ShipType"].title() if "ShipType_Localised" not in j else j["ShipType_Localised"]
                logevent(msg_term=f"Swapped ship to {track.cmdrship}",
                        emoji="🚢", timestamp=logtime, loglevel=2)
            case "Shutdown":
                logevent(msg_term="Quit to desktop",
                        emoji="🛑", timestamp=logtime, loglevel=2)
                track.sessionend()
                if __name__ == "__main__" and not track.preloading and not follow_journals:
                    sys.exit()
            case "SupercruiseEntry" | "FSDJump":
                if j["event"] == "SupercruiseEntry":
                    event = "Supercruise entry in"
                    emoji = "🚀"
                else:
                    event = "FSD jump to"
                    emoji = "☀️"
                logevent(msg_term=f"{event} {j['StarSystem']}",
                        emoji=emoji, timestamp=logtime, loglevel=2)
                track.sessionend()
        track.lasteventname = j["event"]
    except Exception as e:
        event = j["event"] if "event" in j else UNKNOWN
        logtime = datetime.strftime(logtime, "%H:%M:%S") if logtime else UNKNOWN
        msg.log(f"{Col.WARN}Warning:{Col.END} Process event error for [{event}]: {e} (logtime: {logtime})")
        debug(line)

def time_format(seconds: int) -> str:
	if seconds is not None and seconds >= 0:
		seconds = int(seconds)
		h = seconds // 3600
		m = seconds % 3600 // 60
		s = seconds % 3600 % 60
		if h > 0:
			return '{:d}h{:d}m'.format(h, m)
		elif m > 0:
			return '{:d}m{:d}s'.format(m, s)
		else:
			return '{:d}s'.format(s)
	else:
		return "-"

def num_format(number: int) -> str:
    if number is not None:
        number = int(number)
        if number >= 999_500:
            return f"{round(number / 1_000_000, 1):g}m"
        elif number >= 1_000:
            return f"{round(number / 1_000):g}k"
        else:
            return number

def update_status(reset=False):
    """Outputs realtime info to status line and window title"""
    
    if conf_settings["LiveStatus"] or conf_settings["DynamicTitle"]:
        if not track.preloading and track.deploytime:
            # Time
            time_utc = datetime.now(timezone.utc)
            log_time = datetime.now(timezone.utc) if conf_settings["UseUTC"] else datetime.now().astimezone()
            time_clock = datetime.strftime(log_time, "%H:%M:%S")
            session_secs = int((time_utc - track.deploytime).total_seconds())
            time_sesh = time_format(session_secs)

            # Kills
            if session.kills > 0:
                kills_hour = per_hour((time_utc - track.deploytime).total_seconds() / session.kills, 1)
            else:
                kills_hour = "-"
            
            if session.lastkillmono:
                last_kill = time_format(time.monotonic() - session.lastkillmono)
            elif session.kills > 0:
                last_kill = time_format((time_utc - session.lastkillutc).total_seconds())
            else:
                last_kill = time_format((time_utc - track.deploytime).total_seconds())
            
            # Scans
            if session.scansin > 0:
                scans_hour = per_hour((time_utc - track.deploytime).total_seconds() / session.scansin, 1)
            else:
                scans_hour = "-"
            
            if session.lastscanmono:
                lastscan = time_format(time.monotonic() - session.lastscanmono)
            elif session.scansin > 0:
                lastscan = time_format((time_utc - session.lastscanutc).total_seconds())
            else:
                lastscan = time_format((time_utc - track.deploytime).total_seconds())

            # Recent rates
            intervals = conf_settings["RecentIntervals"]
            kills_hour_recent = recent_rates(session.kill_times, None, intervals)
            scans_hour_recent = recent_rates(session.scan_times, None, intervals)

            # Status bar
            show_alt = (int(time.time()) // 5) % 2 if conf_settings["ExtendedStats"] else None
            
            if show_alt and kills_hour_recent:
                s_kills = kills_hour_recent
            else:
                s_kills = f"{kills_hour}/h (+{last_kill}) [x{session.kills}]"
            
            if show_alt and scans_hour_recent:
                s_scans = scans_hour_recent
            else:
                s_scans = f"{scans_hour}/h (+{lastscan}) [x{session.scansin}]"
            
            s_missions = mission_ledger.status_line()
            
            if conf_settings["LiveStatus"]:
                msg.set_status(f"{status_col}[{time_clock:>8}]💥 {s_kills:<23} | 📦 {s_scans:<23} | ⏱️ {time_sesh:<5} | 🎯 {s_missions}")
            
            # Window title (Windows only)
            if conf_settings["DynamicTitle"] and os.name=="nt" and not msg.plain:
                cmdr_name = "" if not conf_settings["WinTitleCmdrName"] else f"{track.cmdrname} "
                ctypes.windll.kernel32.SetConsoleTitleW(f"{cmdr_name}💥{kills_hour}/h ⌚{last_kill} 🎯 {s_missions}")

        elif reset:
            if conf_settings["DynamicTitle"] and os.name=="nt" and not msg.plain:
                ctypes.windll.kernel32.SetConsoleTitleW(f"ED AFK Monitor v{VERSION}")
            if conf_settings["LiveStatus"]:
                msg.clear_status()

# Output stats for kills, bounties and merits
def summary(stats, logtime=None, session=True, live=False):
    kill_type = track.killtype.capitalize()
    log_levels = {"Kills": conf_log_levels["SummaryKills"], "Faction": conf_log_levels["SummaryFaction"],
                  kill_type: conf_log_levels["SummaryBounties"], "Merits": conf_log_levels["SummaryMerits"],
                  "Scans": conf_log_levels["SummaryScans"]}
    mission_level = int(conf_log_levels.get("SummaryMissions", 0))
    log_max = max(list(log_levels.values()) + [mission_level])
    
    if stats.kills < 2 or log_max == 0:
        return
    
    stats_out = {}
    if session:
        session_seconds = (logtime - track.deploytime).total_seconds()
    else:
        session_seconds = track.totaltime
    
    # Shared function for cargo scan and kill summaries
    def recents(duration, count, events):
        average_time = duration / count
        hourly_rate = per_hour(average_time, 1)
        
        recent = ""
        
        # Recent rates
        if session and conf_settings["ExtendedStats"]:
            intervals = conf_settings["RecentIntervals"]
            recents = recent_rates(events, logtime, intervals)
            if recents:
                recent = f" [{recents}]"
        
        return f"{count:,} ({hourly_rate}/h | {time_format(average_time)}){recent}"
    
    # Kills
    if log_levels["Kills"] > 0:
        stats_out["Kills"] = recents(session_seconds, stats.kills, stats.kill_times)
    
    # Faction #1 kills
    faction_kills = max(stats.factions.values())
    if log_levels["Faction"] > 0 and faction_kills > 1:
        faction_kills = max(stats.factions.values())
        faction_kills_hour = per_hour(session_seconds / (faction_kills - 1), 1)
        faction_kills_percent = round((faction_kills / stats.kills) * 100)
        faction_name = max(stats.factions, key=stats.factions.get)

        stats_out["Faction"] = f"{faction_kills:,} ({faction_kills_hour}/h | {faction_kills_percent}%) [{faction_name}]"

    # Cargo scans
    if log_levels["Scans"] > 0 and stats.scansin > 1:
        stats_out["Scans"] = recents(session_seconds, stats.scansin, stats.scan_times)
    
    # Bounties
    if log_levels[kill_type] > 0:
        bounties_hour = per_hour(session_seconds / stats.bounties)
        bounties_average = num_format(stats.bounties // stats.kills)

        stats_out[kill_type] = f"{num_format(stats.bounties)} ({num_format(bounties_hour)}/h | {bounties_average}/kill)"
    
    # Merits
    if log_levels["Merits"] > 0 and stats.merits > 0:
        merits_hour = per_hour(session_seconds / stats.merits) if stats.merits > 0 else 0
        merits_average = round(stats.merits / stats.kills, 1)
        
        stats_out["Merits"] = f"{stats.merits:,} ({merits_hour:,}/h | {merits_average:,}/kill)"
    
    # Output
    if stats_out:
        type = "Session" if session else "Total"
        
        session_time = f" ({time_format(session_seconds)})"
        missions_completed = f" [{mission_ledger.status_line()}]" if mission_ledger.has_missions() else ""
        
        out_terminal = f"{type} Stats{session_time}{missions_completed}"
        out_discord = f"**{type} Stats**{session_time}{missions_completed}"
        out_telegram = out_terminal
        
        for k, v in stats_out.items():
            if log_levels[k] >= 1:
                out_terminal += f"\n{' '*10}-> {k}: {v}"
            if log_levels[k] >= 2:
                out_discord += f"\n:white_small_square: **{k}:** {v}"
                out_telegram += f"\n  -> {k}: {v}"

        if mission_level > 0 and mission_ledger.has_missions():
            mission_text = mission_ledger.report_text()
            out_terminal += f"\n\n{mission_text}"
            if mission_level >= 2:
                out_discord += f"\n\n{mission_ledger.report_live()}"
                out_telegram += f"\n\n{mission_text}"

        if not live:
            logevent(msg_term=f"{out_terminal}",
                msg_discord=f"{out_discord}", msg_telegram=out_telegram,
                emoji="📝", timestamp=logtime, loglevel=log_max)
        else:
            discord_ts = round(time.time())
            live_stats = f"{out_discord}\n-# Updated: <t:{discord_ts}:t> (<t:{discord_ts}:R>)"
            discord_live_update(live_stats)

def journal_rotated(path):
    global journal_file
    journal_file = path.name
    track.lines = 0
    logevent(msg_term=f"Following journal {journal_file}", emoji="📖", loglevel=2)


if __name__ == "__main__":
    try:
        msg.hide_cursor()
        
        preload_position = 0
        # Journal preloading
        if track.preloading:
            mission_ledger.preloading = True
            with open(journal_dir / journal_file, mode="r", encoding="utf-8", errors="replace") as file:
                while True:
                    line = read_complete_line(file)
                    if not line:
                        break
                    processevent(line)
                    track.lines += 1
                preload_position = file.tell()
            track.preloading = False
            mission_ledger.end_preload()
            if args.resetsession:
                session.reset()
                track.sessionstart(reset=True)
                logevent(msg_term="Session stats reset",
                        emoji="🔄", loglevel=1)
            update_status(True)

        # Queue startup notifications
        update_notice = f"\n:arrow_up: Update **[v{latest_version}](https://github.com/{github_repo}/releases)** available!" if VERSION < latest_version else ""

        startup_message = f"💥 **ED AFK Monitor** 💥 by CMDR PSIPAB ([v{VERSION}](https://github.com/{github_repo})){update_notice}"
        if conf_discord["ForumChannel"] and conf_discord["UserID"]:
            startup_message += f" <@{conf_discord['UserID']}>"
        sendmessage(startup_message,
                    telegram_message=f"ED AFK Monitor v{VERSION} started for {track.cmdrname}")

        logevent(msg_term=f"Monitor started ({journal_file})",
                    msg_discord=f"**Monitor started** ({journal_file})",
                    emoji="📖", loglevel=2)
        
        # Resume after history, including events appended during startup.
        trackingerror = None
        cooldown = conf_settings["WarnCooldown"]
        
        with JournalReader(journal_dir / journal_file, position=preload_position,
                           follow=follow_journals, on_rotate=journal_rotated) as file:

            while True:
                notification_reports()
                line = file.readline()
                if not line:
                    try:
                        if track.deploytime:
                            timemono = time.monotonic()
                            
                            # Check for instance problems every minute
                            if not track.lastcheck or timemono - track.lastcheck >= 60:
                                timeutc = datetime.now(timezone.utc)
                                sessionsecs = (timeutc - track.deploytime).total_seconds()
                                if sessionsecs == 0:
                                    sessionsecs = 1	# Avoid divide-by-zero if session started by first kill
                                #if track.lastcheck: debug(f"Last: {track.lastcheck} / This: {timemono} / Drift: {60-(timemono - track.lastcheck)}")
                                timemono = timemono + (60 - (timemono - track.lastcheck)) if track.lastcheck else timemono	# Account for drift
                                track.lastcheck = timemono
                                
                                if session.kills:
                                    # Clear last warned time if past cooldown
                                    if track.warnedkillrate and timemono - track.warnedkillrate >= (cooldown * 60):
                                        cooldown *= 2
                                        track.warnedkillrate = None
                                    
                                    # Check average kill rate
                                    kills_hour = per_hour(sessionsecs / session.kills, 1)
                                    #debug(f"Kills per hour {kills_hour}")
                                    if kills_hour < conf_settings["WarnKillRate"]:
                                        if not track.warnedkillrate and sessionsecs >= (conf_settings["WarnKillRateDelay"] * 60) and (not track.warnednokills or timemono - track.warnednokills >= (cooldown * 60)):
                                            logevent(msg_term=f"Kill rate of {kills_hour}/h is below {conf_settings['WarnKillRate']}/h threshold",
                                                    emoji="⚠️", loglevel=conf_log_levels["KillRate"])
                                            track.warnedkillrate = timemono
                                    else:
                                    # Check time since last kill
                                        lastkill = int((timeutc - session.lastkillutc).total_seconds() / 60)
                                        #debug(f"timeutc: {timeutc} | lastkill: {lastkill} | track.warnedkillrate: {track.warnedkillrate} | conf_settings["WarnNoKills"]: {conf_settings["WarnNoKills"]}")
                                        if not track.warnedkillrate and lastkill >= (conf_settings["WarnNoKills"]):
                                            logevent(msg_term=f"Last logged kill was {lastkill} minutes ago",
                                                emoji="⚠️", loglevel=conf_log_levels["NoKills"])
                                            track.warnedkillrate = timemono
                                else:
                                    # Clear last warned time if past cooldown
                                    if track.warnednokills and timemono - track.warnednokills >= (cooldown * 60):
                                        cooldown *= 2
                                        track.warnednokills = None

                                    # Check time since deployment if no kills yet
                                    sessionmins = int(sessionsecs / 60)
                                    #debug(f"No kills logged since start of session {sessionmins} ({sessionsecs / 60}) minutes ago [conf_settings["WarnNoKillsInitial"]*60: {conf_settings["WarnNoKillsInitial"] * 60}]")
                                    if not track.warnednokills and sessionsecs >= (conf_settings["WarnNoKillsInitial"] * 60):
                                        logevent(msg_term=f"No kills logged for {sessionmins} minutes",
                                                emoji="⚠️", loglevel=conf_log_levels["NoKills"])
                                        track.warnednokills = timemono
                            
                                # Warn about unfinished missions approaching or past their expiry
                                if conf_settings["WarnMissionExpiry"] and mission_ledger.has_missions():
                                    for mission, minutes_left in mission_ledger.expiring(conf_settings["WarnMissionExpiry"]):
                                        if mission.mission_id in track.warned_missions:
                                            continue
                                        track.warned_missions.add(mission.mission_id)
                                        if minutes_left < 0:
                                            detail = f"expired {-minutes_left}m ago and is still open"
                                        else:
                                            target = f" ({mission.target})" if mission.target else ""
                                            detail = f"expires in {minutes_left}m{target}"
                                        logevent(msg_term=f"{Col.WARN}Mission {detail}{Col.END}",
                                                 emoji="⏳", timestamp=datetime.now(timezone.utc),
                                                 loglevel=conf_log_levels["MissionExpiry"])

                            # Send a session summary update every minute
                            if webhook_live and (not track.live_update or timemono - track.live_update >= 60):
                                summary(session, logtime=datetime.now(timezone.utc), live=True)
                                track.live_update = timemono
                    except Exception as e:
                        if repr(e) != trackingerror:
                            msg.log(f"{Col.WARN}Warning:{Col.END} Kill rate tracking error: {e} [{datetime.strftime(datetime.now(), '%H:%M:%S')}])")
                            trackingerror = repr(e)
                    
                    time.sleep(1)
                    mission_ledger.maybe_save()
                    update_status()
                    continue

                processevent(line)
                track.lines += 1

    except (KeyboardInterrupt, SystemExit):
        track.sessionend()
        mission_ledger.save()
        summary(total, session=False)
        logevent(msg_term=f"Monitor stopped ({journal_file})",
            msg_discord=f"**Monitor stopped** ({journal_file})",
            emoji="📕", loglevel=2)
        debug(f"\nTrack: {track.__dict__}")
        msg.show_cursor()
        
        notifications.close()
        notification_reports()
        if sys.argv[0].count("\\") > 1:
            input("\nPress ENTER to exit")	# This is *still* horrible
            sys.exit()
    except Exception as e:
        msg.log(f"{Col.WARN}Warning:{Col.END} Something went wrong: {e} (journal line #{track.lines})")
        traceback.print_tb(e.__traceback__)
        notifications.close()
        notification_reports()
        msg.show_cursor()
        input("Press ENTER to exit")

    finally:
        mission_ledger.save()
        notifications.close()
        notification_reports()
        msg.show_cursor()
