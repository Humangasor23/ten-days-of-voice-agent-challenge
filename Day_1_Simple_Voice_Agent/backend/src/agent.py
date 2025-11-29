# Improved Voice Game Master Agent (Brinmere) - Enhanced features
# - Better intent resolution (keyword+similarity)
# - Help and export tools
# - Persistence of sessions to disk
# - Duplicate-effect protection and safe effect handling
# - Shorter TTS-friendly replies and optional verbose mode
# - Minor performance/logging improvements

import json
import logging
import os
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Annotated

from dotenv import load_dotenv
from pydantic import Field
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
    function_tool,
    RunContext,
)

from livekit.plugins import murf, silero, google, deepgram, noise_cancellation
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# -------------------------
# Logging
# -------------------------
logger = logging.getLogger("voice_game_master")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

load_dotenv(".env.local")

# -------------------------
# Helper: simple fuzzy matching using token overlap
# (keeps everything local and deterministic; no external deps)
# -------------------------
import re

def tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"\W+", text.lower()) if t]


def similarity(a: str, b: str) -> float:
    # Jaccard-like token overlap
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    inter = ta.intersection(tb)
    union = ta.union(tb)
    return len(inter) / len(union)

# -------------------------
# Simple Game World (unchanged, but could be loaded from JSON)
# -------------------------
WORLD = {
    "intro": {
        "title": "A Shadow over Brinmere",
        "desc": (
            "You awake on the damp shore of Brinmere, the moon a thin silver crescent. "
            "A ruined watchtower smolders a short distance inland, and a narrow path leads "
            "towards a cluster of cottages to the east. In the water beside you lies a "
            "small, carved wooden box, half-buried in sand."
        ),
        "choices": {
            "inspect_box": {
                "desc": "Inspect the carved wooden box at the water's edge.",
                "result_scene": "box",
            },
            "approach_tower": {
                "desc": "Head inland towards the smoldering watchtower.",
                "result_scene": "tower",
            },
            "walk_to_cottages": {
                "desc": "Follow the path east towards the cottages.",
                "result_scene": "cottages",
            },
        },
    },
    "box": {
        "title": "The Box",
        "desc": (
            "The box is warm despite the night air. Inside is a folded scrap of parchment "
            "with a hatch-marked map and the words: 'Beneath the tower, the latch sings.' "
            "As you read, a faint whisper seems to come from the tower, as if the wind "
            "itself speaks your name."
        ),
        "choices": {
            "take_map": {
                "desc": "Take the map and keep it.",
                "result_scene": "tower_approach",
                "effects": {"add_journal": "Found map fragment: 'Beneath the tower, the latch sings.'"},
            },
            "leave_box": {
                "desc": "Leave the box where it is.",
                "result_scene": "intro",
            },
        },
    },
    "tower": {
        "title": "The Watchtower",
        "desc": (
            "The watchtower's stonework is cracked and warm embers glow within. An iron "
            "latch covers a hatch at the base — it looks old but recently used. You can "
            "try the latch, look for other entrances, or retreat."
        ),
        "choices": {
            "try_latch_without_map": {
                "desc": "Try the iron latch without any clue.",
                "result_scene": "latch_fail",
            },
            "search_around": {
                "desc": "Search the nearby rubble for another entrance.",
                "result_scene": "secret_entrance",
            },
            "retreat": {
                "desc": "Step back to the shoreline.",
                "result_scene": "intro",
            },
        },
    },
    "tower_approach": {
        "title": "Toward the Tower",
        "desc": (
            "Clutching the map, you approach the watchtower. The map's marks align with "
            "the hatch at the base, and you notice a faint singing resonance when you step close."
        ),
        "choices": {
            "open_hatch": {
                "desc": "Use the map clue and try the hatch latch carefully.",
                "result_scene": "latch_open",
                "effects": {"add_journal": "Used map clue to open the hatch."},
            },
            "search_around": {
                "desc": "Search for another entrance.",
                "result_scene": "secret_entrance",
            },
            "retreat": {
                "desc": "Return to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "latch_fail": {
        "title": "A Bad Twist",
        "desc": (
            "You twist the latch without heed — the mechanism sticks, and the effort sends "
            "a shiver through the ground. From inside the tower, something rustles in alarm."
        ),
        "choices": {
            "run_away": {
                "desc": "Run back to the shore.",
                "result_scene": "intro",
            },
            "stand_ground": {
                "desc": "Stand and prepare for whatever emerges.",
                "result_scene": "tower_combat",
            },
        },
    },
    "latch_open": {
        "title": "The Hatch Opens",
        "desc": (
            "With the map's guidance the latch yields and the hatch opens with a breath of cold air. "
            "Inside, a spiral of rough steps leads down into an ancient cellar lit by phosphorescent moss."
        ),
        "choices": {
            "descend": {
                "desc": "Descend into the cellar.",
                "result_scene": "cellar",
            },
            "close_hatch": {
                "desc": "Close the hatch and reconsider.",
                "result_scene": "tower_approach",
            },
        },
    },
    "secret_entrance": {
        "title": "A Narrow Gap",
        "desc": (
            "Behind a pile of rubble you find a narrow gap and old rope leading downward. "
            "It smells of cold iron and something briny."
        ),
        "choices": {
            "squeeze_in": {
                "desc": "Squeeze through the gap and follow the rope down.",
                "result_scene": "cellar",
            },
            "mark_and_return": {
                "desc": "Mark the spot and return to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "cellar": {
        "title": "Cellar of Echoes",
        "desc": (
            "The cellar opens into a circular chamber where runes glow faintly. At the center "
            "is a stone plinth and upon it a small brass key and a sealed scroll."
        ),
        "choices": {
            "take_key": {
                "desc": "Pick up the brass key.",
                "result_scene": "cellar_key",
                "effects": {"add_inventory": "brass_key", "add_journal": "Found brass key on plinth."},
            },
            "open_scroll": {
                "desc": "Break the seal and read the scroll.",
                "result_scene": "scroll_reveal",
                "effects": {"add_journal": "Scroll reads: 'The tide remembers what the villagers forget.'"},
            },
            "leave_quietly": {
                "desc": "Leave the cellar and close the hatch behind you.",
                "result_scene": "intro",
            },
        },
    },
    "cellar_key": {
        "title": "Key in Hand",
        "desc": (
            "With the key in your hand the runes dim and a hidden panel slides open, revealing a "
            "small statue that begins to hum. A voice, ancient and kind, asks: 'Will you return what was taken?'"
        ),
        "choices": {
            "pledge_help": {
                "desc": "Pledge to return what was taken.",
                "result_scene": "reward",
                "effects": {"add_journal": "You pledged to return what was taken."},
            },
            "refuse": {
                "desc": "Refuse and pocket the key.",
                "result_scene": "cursed_key",
                "effects": {"add_journal": "You pocketed the key; a weight grows in your pocket."},
            },
        },
    },
    "scroll_reveal": {
        "title": "The Scroll",
        "desc": (
            "The scroll tells of an heirloom taken by a water spirit that dwells beneath the tower. "
            "It hints that the brass key 'speaks' when offered with truth."
        ),
        "choices": {
            "search_for_key": {
                "desc": "Search the plinth for a key.",
                "result_scene": "cellar_key",
            },
            "leave_quietly": {
                "desc": "Leave the cellar and keep the knowledge to yourself.",
                "result_scene": "intro",
            },
        },
    },
    "tower_combat": {
        "title": "Something Emerges",
        "desc": (
            "A hunched, brine-soaked creature scrambles out from the tower. Its eyes glow with hunger. "
            "You must act quickly."
        ),
        "choices": {
            "fight": {
                "desc": "Fight the creature.",
                "result_scene": "fight_win",
            },
            "flee": {
                "desc": "Flee back to the shore.",
                "result_scene": "intro",
            },
        },
    },
    "fight_win": {
        "title": "After the Scuffle",
        "desc": (
            "You manage to fend off the creature; it flees wailing towards the sea. On the ground lies "
            "a small locket engraved with a crest — likely the heirloom mentioned in the scroll."
        ),
        "choices": {
            "take_locket": {
                "desc": "Take the locket and examine it.",
                "result_scene": "reward",
                "effects": {"add_inventory": "engraved_locket", "add_journal": "Recovered an engraved locket."},
            },
            "leave_locket": {
                "desc": "Leave the locket and tend to your wounds.",
                "result_scene": "intro",
            },
        },
    },
    "reward": {
        "title": "A Minor Resolution",
        "desc": (
            "A small sense of peace settles over Brinmere. Villagers may one day know the heirloom is found, or it may remain a secret. "
            "You feel the night shift; the little arc of your story here closes for now."
        ),
        "choices": {
            "end_session": {
                "desc": "End the session and return to the shore (conclude mini-arc).",
                "result_scene": "intro",
            },
            "keep_exploring": {
                "desc": "Keep exploring for more mysteries.",
                "result_scene": "intro",
            },
        },
    },
    "cursed_key": {
        "title": "A Weight in the Pocket",
        "desc": (
            "The brass key glows coldly. You feel a heavy sorrow that tugs at your thoughts. "
            "Perhaps the key demands something in return..."
        ),
        "choices": {
            "seek_redemption": {
                "desc": "Seek a way to make amends.",
                "result_scene": "reward",
            },
            "bury_key": {
                "desc": "Bury the key and hope the weight fades.",
                "result_scene": "intro",
            },
        },
    },
}

# Note: For brevity in the canvas version this module will include the full WORLD object.
# When deploying, keep WORLD in a separate JSON file and load it at startup to ease edits.

# -------------------------
# Per-session Userdata
# -------------------------
@dataclass
class Userdata:
    player_name: Optional[str] = None
    current_scene: str = "intro"
    history: List[Dict] = field(default_factory=list)
    journal: List[str] = field(default_factory=list)
    inventory: List[str] = field(default_factory=list)
    named_npcs: Dict[str, str] = field(default_factory=dict)
    choices_made: List[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    verbose: bool = False  # toggle verbose descriptions for debugging or desktop usage

    def to_json(self) -> dict:
        return {
            "player_name": self.player_name,
            "current_scene": self.current_scene,
            "history": self.history,
            "journal": self.journal,
            "inventory": self.inventory,
            "named_npcs": self.named_npcs,
            "choices_made": self.choices_made,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "verbose": self.verbose,
        }

    @staticmethod
    def from_json(d: dict) -> 'Userdata':
        ud = Userdata()
        ud.player_name = d.get("player_name")
        ud.current_scene = d.get("current_scene", "intro")
        ud.history = d.get("history", [])
        ud.journal = d.get("journal", [])
        ud.inventory = d.get("inventory", [])
        ud.named_npcs = d.get("named_npcs", {})
        ud.choices_made = d.get("choices_made", [])
        ud.session_id = d.get("session_id", ud.session_id)
        ud.started_at = d.get("started_at", ud.started_at)
        ud.verbose = d.get("verbose", False)
        return ud

# -------------------------
# Persistence helpers (save/load userdata)
# -------------------------
SESSIONS_DIR = "./sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, f"session_{session_id}.json")


def save_session(userdata: Userdata):
    try:
        with open(session_path(userdata.session_id), "w", encoding="utf-8") as f:
            json.dump(userdata.to_json(), f, ensure_ascii=False, indent=2)
        logger.info(f"Saved session {userdata.session_id}")
    except Exception as e:
        logger.warning(f"Failed to save session: {e}")


def load_session(session_id: str) -> Optional[Userdata]:
    p = session_path(session_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Userdata.from_json(data)
    except Exception as e:
        logger.warning(f"Failed to load session {session_id}: {e}")
        return None

# -------------------------
# Scene rendering & effects
# -------------------------

def scene_text(scene_key: str, userdata: Userdata) -> str:
    scene = WORLD.get(scene_key)
    if not scene:
        return "You are in a featureless void. What do you do?"

    base = scene.get("desc", "")
    # keep choices short for voice delivery
    choices = []
    for cid, cmeta in scene.get("choices", {}).items():
        short = cmeta.get("desc", "")
        choices.append(f"{short} — say: {cid}")

    desc = base
    if userdata.verbose:
        desc += "\n\nChoices:\n" + "\n".join([f"- {c}" for c in choices])
    else:
        # short hint list
        desc += "\n\nYou can: " + ", ".join([c.split('—')[0].strip() for c in choices])

    # always end with the prompt for voice flow
    if not desc.strip().endswith("What do you do?"):
        desc += "\n\nWhat do you do?"
    return desc


def apply_effects(effects: dict, userdata: Userdata):
    if not effects:
        return
    # protect against duplicates
    if "add_journal" in effects:
        entry = effects["add_journal"]
        if entry not in userdata.journal:
            userdata.journal.append(entry)
    if "add_inventory" in effects:
        item = effects["add_inventory"]
        if item not in userdata.inventory:
            userdata.inventory.append(item)

# -------------------------
# Transition logging
# -------------------------

def summarize_scene_transition(old_scene: str, action_key: str, result_scene: str, userdata: Userdata) -> str:
    entry = {
        "from": old_scene,
        "action": action_key,
        "to": result_scene,
        "time": datetime.utcnow().isoformat() + "Z",
    }
    userdata.history.append(entry)
    userdata.choices_made.append(action_key)
    # Save after each state mutation to reduce data loss
    save_session(userdata)
    return f"You chose '{action_key}'."

# -------------------------
# Tools (start, get_scene, player_action, show_journal, restart, help, export)
# -------------------------

@function_tool
async def start_adventure(
    ctx: RunContext[Userdata],
    player_name: Annotated[Optional[str], Field(description="Player name", default=None)] = None,
    resume_session: Annotated[Optional[str], Field(description="Session id to resume (optional)", default=None)] = None,
) -> str:
    userdata = ctx.userdata
    # If resume requested, attempt to load
    if resume_session:
        loaded = load_session(resume_session)
        if loaded:
            ctx.userdata = loaded
            userdata = loaded
            return f"Resumed session {userdata.session_id}.\n\n" + scene_text(userdata.current_scene, userdata)

    if player_name:
        userdata.player_name = player_name
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"

    # Save an initial snapshot
    save_session(userdata)

    opening = (
        f"Greetings {userdata.player_name or 'traveler'}. Welcome to '{WORLD['intro']['title']}'.\n\n"
        + scene_text("intro", userdata)
    )
    return opening


@function_tool
async def get_scene(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    scene_k = userdata.current_scene or "intro"
    txt = scene_text(scene_k, userdata)
    return txt


@function_tool
async def player_action(
    ctx: RunContext[Userdata],
    action: Annotated[str, Field(description="Player spoken action or the short action code (e.g., 'inspect_box')")],
) -> str:
    userdata = ctx.userdata
    current = userdata.current_scene or "intro"
    scene = WORLD.get(current)
    action_text = (action or "").strip()

    if not scene:
        return "Strange — your surroundings are unclear. Try 'start_adventure' to begin.\n\nWhat do you do?"

    # 1) direct key match
    chosen_key = None
    lowered = action_text.lower()
    if lowered in scene.get("choices", {}):
        chosen_key = lowered

    # 2) exact description match
    if not chosen_key:
        for cid, cmeta in scene.get("choices", {}).items():
            if cmeta.get("desc", "").lower() == lowered:
                chosen_key = cid
                break

    # 3) similarity scoring
    if not chosen_key:
        best = (None, 0.0)
        for cid, cmeta in scene.get("choices", {}).items():
            score = max(
                similarity(action_text, cid),
                similarity(action_text, cmeta.get("desc", "")),
            )
            if score > best[1]:
                best = (cid, score)
        # threshold tuned for short voice phrases
        if best[1] >= 0.25:
            chosen_key = best[0]

    # 4) fallback keyword containment
    if not chosen_key:
        for cid, cmeta in scene.get("choices", {}).items():
            for token in tokenize(cmeta.get("desc", "")[:80]):
                if token in lowered:
                    chosen_key = cid
                    break
            if chosen_key:
                break

    if not chosen_key:
        resp = (
            "I didn't quite catch that action for this situation. Try one of the listed choices or say something simple like 'inspect the box' or 'go to the tower'.\n\n"
            + scene_text(current, userdata)
        )
        return resp

    choice_meta = scene['choices'].get(chosen_key)
    result_scene = choice_meta.get('result_scene', current)
    effects = choice_meta.get('effects', None)

    # Apply effects safely
    apply_effects(effects or {}, userdata)

    # Record transition
    _note = summarize_scene_transition(current, chosen_key, result_scene, userdata)

    userdata.current_scene = result_scene

    # Build reply — keep concise for TTS and voice latency
    next_desc = scene_text(result_scene, userdata)

    persona_pre = "Aurek (the Game Master) says:\n\n"
    reply = f"{persona_pre}{_note}\n\n{next_desc}"
    # Save after producing reply
    save_session(userdata)
    return reply


@function_tool
async def show_journal(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    lines = []
    lines.append(f"Session: {userdata.session_id} | Started at: {userdata.started_at}")
    if userdata.player_name:
        lines.append(f"Player: {userdata.player_name}")
    if userdata.journal:
        lines.append("\nJournal entries:")
        for j in userdata.journal:
            lines.append(f"- {j}")
    else:
        lines.append("\nJournal is empty.")
    if userdata.inventory:
        lines.append("\nInventory:")
        for it in userdata.inventory:
            lines.append(f"- {it}")
    else:
        lines.append("\nNo items in inventory.")
    lines.append("\nRecent choices:")
    for h in userdata.history[-6:]:
        lines.append(f"- {h['time']} | from {h['from']} -> {h['to']} via {h['action']}")
    lines.append("\nWhat do you do?")
    return "\n".join(lines)


@function_tool
async def restart_adventure(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"
    save_session(userdata)
    greeting = (
        "The world resets. A new tide laps at the shore. You stand once more at the beginning.\n\n"
        + scene_text("intro", userdata)
    )
    return greeting


@function_tool
async def help_adventure(
    ctx: RunContext[Userdata],
) -> str:
    return (
        "I am Aurek, your Game Master.\n"
        "You can do things like: 'inspect box', 'go to the tower', or say the short codes shown after each choice (e.g., 'inspect_box').\n"
        "Tools available: start_adventure(player_name), get_scene(), player_action(action), show_journal(), restart_adventure(), export_session()\n\n"
        "What do you do?"
    )


@function_tool
async def export_session(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    p = session_path(userdata.session_id)
    if not os.path.exists(p):
        save_session(userdata)
    return f"Session exported to: {p}"

# -------------------------
# Agent class
# -------------------------
class GameMasterAgent(Agent):
    def __init__(self):
        instructions = """
        You are 'Aurek', the Game Master (GM) for a voice-only, Dungeons-and-Dragons-style short adventure.
        Universe: Low-magic coastal fantasy (village of Brinmere, tide-smoothed ruins, minor spirits).
        Tone: Slightly mysterious, dramatic, empathetic (not overly scary).
        Role: You are the GM. You describe scenes vividly, remember the player's past choices, named NPCs, inventory and locations,
              and you always end your descriptive messages with the prompt: 'What do you do?'
        Rules:
            - Use the provided tools to start the adventure, get the current scene, accept the player's spoken action,
              show the player's journal, restart the adventure, export the session, or show help.
            - Keep continuity using the per-session userdata. Reference journal items and inventory when relevant.
            - Drive short sessions (aim for several meaningful turns). Each GM message MUST end with 'What do you do?'.
            - Respect that this agent is voice-first: responses should be concise enough for spoken delivery but evocative.
        """
        super().__init__(
            instructions=instructions,
            tools=[
                start_adventure,
                get_scene,
                player_action,
                show_journal,
                restart_adventure,
                help_adventure,
                export_session,
            ],
        )

# -------------------------
# Entrypoint & Prewarm
# -------------------------

def prewarm(proc: JobProcess):
    try:
        proc.userdata["vad"] = silero.VAD.load()
    except Exception:
        logger.warning("VAD prewarm failed; continuing without preloaded VAD.")


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    logger.info("\n" + "🎲" * 8)
    logger.info("🚀 STARTING VOICE GAME MASTER (Brinmere Mini-Arc) - Improved")

    # Try to resume an existing session if set on process userdata
    userdata = Userdata()
    session_id = ctx.proc.userdata.get("resume_session")
    if session_id:
        loaded = load_session(session_id)
        if loaded:
            userdata = loaded
            logger.info(f"Resumed session {session_id}")

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=google.LLM(model="gemini-2.5-flash"),
        tts=murf.TTS(
            voice="en-US-marcus",
            style="Conversational",
            text_pacing=True,
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata.get("vad"),
        userdata=userdata,
    )

    await session.start(
        agent=GameMasterAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVC()),
    )

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))

"""# --- Added: Turn Counter ---
# In Userdata class definition, ensure there is a 'turns' field defaulting to 0.
# In player_action(), increment state.turns += 1.
# If turns exceed 15, force scene = 'reward' and return mini-arc ending.

# --- Added: FastAPI UI Server ---
# Below is a minimal UI served via FastAPI.
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ui_app = FastAPI()
USER_SESSIONS = {}

ui_html = """
'''
<!DOCTYPE html>
<html>
<head>
<meta charset='UTF-8'>
<title>Voice GM UI</title>
<style>
body { font-family: Arial; background: #111; color: #eee; padding: 20px; }
.box { background:#222; padding:15px; margin-bottom:15px; border-radius:8px; }
button { padding:10px 20px; margin-top:10px; }
input { width:80%; padding:10px; }
</style>
</head>
<body>
<h2>Voice Adventure</h2>
<div class='box'>
<h3>Game Master</h3>
<p id='gm'></p>
</div>
<div class='box'>
<h3>Your Transcript</h3>
<p id='player'></p>
</div>
<div class='box'>
<input id='action' placeholder='Say your action...'/>
<button onclick='sendAction()'>Send</button>
<button onclick='restart()'>Restart Story</button>
</div>
<script>
async function load(){
  let r = await fetch('/api/get');
  let j = await r.json();
  document.getElementById('gm').innerText = j.gm;
  document.getElementById('player').innerText = j.player;
}
async function sendAction(){
  let act = document.getElementById('action').value;
  await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action: act})});
  load();
}
async function restart(){ await fetch('/api/restart'); load(); }
load();
</script>
</body>
</html>
'''

"""

@ui_app.get('/', response_class=HTMLResponse)
def home(): return ui_html

@ui_app.get('/api/get')
def api_get():
    try:
        s = USER_SESSIONS[next(iter(USER_SESSIONS))]
        gm = s.history[-1]['gm'] if s.history else ""
        p = s.history[-1].get('player','') if s.history else ""
    except:
        gm = "Session not started."
        p = ""
    return {"gm": gm, "player": p}

@ui_app.post('/api/action')
def api_action(req: Request):
    import asyncio
    data = asyncio.run(req.json())
    action = data.get('action','')
    # Send through the tool
    session_id = next(iter(USER_SESSIONS))
    return {"result": player_action(action, session_id)}

@ui_app.get('/api/restart')
def api_restart():
    sid = next(iter(USER_SESSIONS))
    restart_adventure(sid)
    return {"ok": True}

# To run UI: uvicorn.run(ui_app, host="0.0.0.0", port=8080)

"""
