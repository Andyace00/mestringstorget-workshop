"""
Mestringstorget Workshop Server
================================
FastAPI + WebSocket server for live digital input fra deltakere.

Arkitektur:
  - Fasilitator-PC kjorer denne serveren
  - Alle deltakere apner http://<fasilitator-IP>:8000/p i nettleser
  - Presentasjonen apner http://<fasilitator-IP>:8000/ (eller localhost)
  - All input lagres i workshop_data.json (auto-saved kontinuerlig)
  - WebSocket gir live-oppdateringer alle veier

Rounds (input-runder):
  1. mok          - Hva tar vi med oss (slide 6)
  2. behold       - BEHOLD/ENDRE/SLIPP sortering (slide 7)
  3. journey      - Karis nye reise (slide 10)
  4. dotvote      - Prioritering (slide 12)
  5. who          - Hvem sitter i mestringstorget (slide 15)
  6. replace      - Hva erstatter vi (slide 16)
  7. format       - Fysisk/digitalt (slide 17)
  8. commit       - Forpliktelsen (slide 19)
  9. consensus    - Konsensussjekk pa hele tavlen (slide 20)

Kjor:
  python workshop_server.py

Apne:
  http://localhost:8000/admin     - Fasilitator-kontroller
  http://localhost:8000/wall      - Live wall (vises pa storskjerm)
  http://localhost:8000/p         - Deltaker-input
  http://localhost:8000/export    - Last ned full rapport
"""
import os
import json
import asyncio
import socket
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Gemini for AI-deduplisering / klyngesortering (valgfri)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
gemini_client = None
if GEMINI_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_KEY)
        print(f"Gemini aktiv for AI-deduplisering")
    except Exception as e:
        print(f"Gemini ikke tilgjengelig: {e}")

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "workshop_data.json"
CONTEXT_FILE = BASE_DIR / "workshop_context.json"


def load_context():
    """Last workshop-kontekst (brukes av AI-deduplisering for treffsikre prompts)."""
    if CONTEXT_FILE.exists():
        try:
            with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Kunne ikke laste workshop_context.json: {e}")
    return {}


WORKSHOP_CONTEXT = load_context()

app = FastAPI(title="Mestringstorget Workshop Server")

# CORS — tillat alle origins (inkl. file:// som kommer som "null") slik at
# presentasjonen kan kalle API-et fra hvor som helst
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # MA vaere False for at allow_origins=["*"] skal funke
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files only if they exist (de finnes lokalt, ikke i sky-deploy)
SCENE_IMAGES_DIR = BASE_DIR / "scene_images"
if SCENE_IMAGES_DIR.exists():
    app.mount("/scene_images", StaticFiles(directory=str(SCENE_IMAGES_DIR)), name="scene_images")

# ---------- DATA MODEL ----------

DEFAULT_ROUNDS = {
    "mok": {
        "title": "Hva vil vi unngå",
        "question": "Hva er du som leder mest opptatt av at vi unngår — når vi nå starter på nytt?",
        "type": "freetext",
        "items": [],
        "active": False,
    },
    # behold-runden (Behold/Endre/Slipp) fjernet 2026-04-12:
    # Slide 6 produserer "unngå"-lapper — alt ville havnet i Slipp. Logisk inkonsistent.
    "carry": {
        "title": "Hva tar vi med oss — styrker og ressurser",
        "question": "Hva er du som leder mest opptatt av at vi TAR MED oss når vi nå starter på nytt?",
        "type": "freetext",
        "items": [],
        "active": False,
    },
    "journey": {
        "title": "Karis nye reise",
        "question": "Design Karis komplette reise gjennom mestringstorget. Hva skjer steg for steg fra hun tar kontakt til hun mestrer hverdagen?",
        "type": "journey",
        "journeys": {},  # user_id -> {"steps": [...], "user_name": str, "ts": str}
        "active": False,
    },
    "dotvote": {
        "title": "Prioritering",
        "question": "Hva er viktigst for oss? Bruk 3 stemmer",
        "type": "vote",
        "options": [],
        "votes": {},  # user_id -> [option_id, ...]
        "active": False,
    },
    "who": {
        "title": "Retningsvalg 1: Hvem sitter i mestringstorget",
        "question": "Hvilken kompetanse må sitte i mestringstorget?",
        "type": "freetext",
        "items": [],
        "consensus": [],  # list of {user_id, value}
        "active": False,
    },
    "replace": {
        "title": "Retningsvalg 2: Hva erstatter vi",
        "question": "Hva bør mestringstorget ERSTATTE av dagens innganger, prosesser og tjenester?",
        "type": "freetext",
        "items": [],
        "consensus": [],
        "active": False,
    },
    "keep": {
        "title": "Retningsvalg 3: Hva beholder vi",
        "question": "Hva bør vi BEHOLDE uendret eller som kjerne inn i mestringstorget?",
        "type": "freetext",
        "items": [],
        "consensus": [],
        "active": False,
    },
    "format": {
        "title": "Retningsvalg 4: Fysisk, digitalt eller begge",
        "question": "Hvordan ser mestringstorget ut? Hvor møter innbyggeren oss?",
        "type": "freetext",
        "items": [],
        "consensus": [],
        "active": False,
    },
    "who_rating": {
        "title": "Konsensussjekk: Hvem sitter i mestringstorget",
        "question": "Hvor godt kan du stille deg bak retningen som kom frem?",
        "type": "rating",
        "ratings": [],
        "parent": "who",
        "active": False,
    },
    "replace_rating": {
        "title": "Konsensussjekk: Hva erstatter vi",
        "question": "Hvor godt kan du stille deg bak retningen som kom frem?",
        "type": "rating",
        "ratings": [],
        "parent": "replace",
        "active": False,
    },
    "keep_rating": {
        "title": "Konsensussjekk: Hva beholder vi",
        "question": "Hvor godt kan du stille deg bak retningen som kom frem?",
        "type": "rating",
        "ratings": [],
        "parent": "keep",
        "active": False,
    },
    "format_rating": {
        "title": "Konsensussjekk: Fysisk, digitalt eller begge",
        "question": "Hvor godt kan du stille deg bak retningen som kom frem?",
        "type": "rating",
        "ratings": [],
        "parent": "format",
        "active": False,
    },
    "commit": {
        "title": "Forpliktelsen",
        "question": "Hva er det viktigste som MÅ skje for at mestringstorget skal bli virkelighet?",
        "type": "freetext",
        "items": [],
        "active": False,
    },
    "consensus": {
        "title": "Konsensussjekk — hele tavlen",
        "question": "Kan du stille deg bak forpliktelsestavlen? (1-5)",
        "type": "rating",
        "ratings": [],  # {user_id, value, comment}
        "active": False,
    },
}


def load_state():
    """Last lagret state og merge inn DEFAULT_ROUNDS sine metadata (title, question, type, parent).
    Bruker-input (items, journeys, ratings, votes, options) beholdes fra lagret data."""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge: sørg for at alle default-runder finnes, og at metadata fra DEFAULT_ROUNDS brukes
            default_copy = json.loads(json.dumps(DEFAULT_ROUNDS))
            saved_rounds = saved.get("rounds", {})
            merged_rounds = {}
            for rid, default_r in default_copy.items():
                if rid in saved_rounds:
                    # Ta metadata fra default (title, question, type, parent, categories, etc.)
                    merged = dict(default_r)
                    # Overstyrer data-feltene fra saved
                    for data_field in ("items", "journeys", "ratings", "votes", "options", "consensus", "active"):
                        if data_field in saved_rounds[rid]:
                            merged[data_field] = saved_rounds[rid][data_field]
                    merged_rounds[rid] = merged
                else:
                    merged_rounds[rid] = default_r
            saved["rounds"] = merged_rounds
            return saved
        except Exception as e:
            print(f"Feil ved lasting: {e}")
    return {
        "session_started": datetime.now().isoformat(),
        "active_round": None,
        "participants": {},  # user_id -> name
        "rounds": json.loads(json.dumps(DEFAULT_ROUNDS)),  # deep copy
    }


def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)


STATE = load_state()


# ---------- WEBSOCKET CONNECTION MANAGER ----------

class ConnectionManager:
    def __init__(self):
        self.connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.discard(ws)

    async def broadcast(self, message: dict):
        dead = set()
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.connections.discard(ws)


manager = ConnectionManager()


# ---------- ROUTES ----------

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


@app.get("/")
async def root():
    return HTMLResponse(LANDING_HTML.replace("{{IP}}", get_local_ip()))


@app.get("/admin", response_class=HTMLResponse)
async def admin():
    return HTMLResponse(ADMIN_HTML)


@app.get("/p", response_class=HTMLResponse)
async def participant():
    return HTMLResponse(PARTICIPANT_HTML)


@app.get("/wall", response_class=HTMLResponse)
async def wall():
    return HTMLResponse(WALL_HTML)


@app.get("/api/state")
async def get_state():
    return JSONResponse(STATE)


@app.get("/api/export")
async def export():
    return JSONResponse(STATE, headers={"Content-Disposition": "attachment; filename=workshop_export.json"})


@app.post("/api/round/{round_id}/activate")
async def activate_round(round_id: str):
    if round_id not in STATE["rounds"]:
        return JSONResponse({"error": "ukjent runde"}, status_code=404)
    for r in STATE["rounds"].values():
        r["active"] = False
    STATE["rounds"][round_id]["active"] = True
    STATE["active_round"] = round_id
    save_state()
    await manager.broadcast({"type": "round_changed", "round_id": round_id, "state": STATE})
    return {"ok": True}


@app.post("/api/round/{round_id}/clear")
async def clear_round(round_id: str):
    if round_id not in STATE["rounds"]:
        return JSONResponse({"error": "ukjent runde"}, status_code=404)
    r = STATE["rounds"][round_id]
    if "items" in r:
        r["items"] = []
    if "votes" in r:
        r["votes"] = {}
    if "ratings" in r:
        r["ratings"] = []
    if "consensus" in r:
        r["consensus"] = []
    if "journeys" in r:
        r["journeys"] = {}
    save_state()
    await manager.broadcast({"type": "round_cleared", "round_id": round_id, "state": STATE})
    return {"ok": True}


def collect_journey_steps_text() -> List[str]:
    """Hent alle steg fra journey-runden som rå tekst (med duplikater)."""
    journeys = STATE["rounds"].get("journey", {}).get("journeys", {})
    out = []
    for j in journeys.values():
        for s in j.get("steps", []):
            s = (s or "").strip()
            if s:
                out.append(s)
    return out


def collect_round_items_text(round_id: str) -> List[str]:
    """Hent alle items fra en runde som rå tekst."""
    r = STATE["rounds"].get(round_id, {})
    return [(it.get("value") or "").strip() for it in r.get("items", []) if (it.get("value") or "").strip()]


def build_dotvote_prompt(raw_journey: List[str], raw_mok: List[str], raw_carry: List[str] = None, min_n: int = 6, max_n: int = 8) -> str:
    """Bygg en rik, kontekstualisert prompt for dot-voting basert på workshop_context.json."""
    raw_carry = raw_carry or []
    ctx = WORKSHOP_CONTEXT
    parts = []

    # ROLLE
    rolle = ctx.get("prompt_instruksjoner_for_dotvote", {}).get("rolle", "")
    if rolle:
        parts.append(f"# ROLLE\n{rolle}")

    # DOMENE
    dom = ctx.get("domene", {})
    if dom:
        parts.append(
            f"# DOMENE — {dom.get('navn', 'Mestringstorget')}\n"
            f"Kilde: {dom.get('kilde', '')}\n\n"
            f"Definisjon: {dom.get('definisjon', '')}\n\n"
            f"Tre spor:\n" + "\n".join(f"- {s}" for s in dom.get('tre_spor', [])) + "\n\n"
            f"Ambisjon: {dom.get('ambisjon', '')}"
        )

    # KONTEKST FOR HELSE OG MESTRING
    hm = ctx.get("kontekst_helse_og_mestring_nfk", {})
    if hm:
        parts.append(
            f"# KONTEKST — Helse og mestring i Nordre Follo\n"
            f"Tjenester som finnes i dag:\n" + "\n".join(f"- {s}" for s in hm.get('tjenester_som_finnes_i_dag', [])) + "\n\n"
            f"VIKTIG: Disse tjenestene finnes IKKE og må ikke nevnes:\n" + "\n".join(f"- {s}" for s in hm.get('tjenester_som_IKKE_finnes', [])) + "\n\n"
            f"Kjente utfordringer:\n" + "\n".join(f"- {s}" for s in hm.get('kjente_utfordringer', []))
        )

    # INSPIRASJON DELTAKERNE HAR SETT
    insp = ctx.get("inspirasjonsmodeller_workshopen_har_sett", [])
    if insp:
        insp_text = "\n".join(f"- {m['navn']}: {m['kjerne']}" for m in insp)
        parts.append(f"# INSPIRASJON DELTAKERNE HAR SETT\nDe har sett tre modeller fra andre kommuner rett før denne avstemningen:\n{insp_text}")

    # WORKSHOP-FLYT
    flyt = ctx.get("workshop_flyt", {})
    dotvote_kontekst = ctx.get("dotvote_kontekst", {})
    if flyt or dotvote_kontekst:
        parts.append(
            f"# HVOR I WORKSHOPEN ER VI NÅ\n"
            f"{dotvote_kontekst.get('hva_skjer_pa_slide_12', '')}\n\n"
            f"Hva kommer etter: {dotvote_kontekst.get('hva_kommer_etter', '')}"
        )

    # HVA ALTERNATIVENE SKAL VÆRE
    alts = dotvote_kontekst.get("hva_alternativene_skal_være", "")
    if alts:
        parts.append(f"# HVA ALTERNATIVENE SKAL VÆRE\n{alts}")

    # TONE OG SPRÅK
    tone = ctx.get("tone_og_språk", {})
    if tone:
        parts.append(
            f"# TONE OG SPRÅK\n"
            f"Skal være:\n" + "\n".join(f"- {s}" for s in tone.get('skal_være', [])) + "\n\n"
            f"Skal unngås:\n" + "\n".join(f"- {s}" for s in tone.get('skal_unngås', []))
        )

    # OPPGAVE OG REGLER
    instr = ctx.get("prompt_instruksjoner_for_dotvote", {})
    if instr:
        parts.append(
            f"# DIN OPPGAVE\n{instr.get('oppgave', '')}\n\n"
            f"Viktige regler:\n" + "\n".join(f"- {s}" for s in instr.get('viktige_regler', []))
        )

    # INPUT — JOURNEY er hovedkilden. carry er RESSURSER. mok er VARSLER.
    parts.append(
        f"# HOVEDINPUT — Karis nye reise (slide 10)\n"
        f"Dette er DESIGN-forslag deltakerne har laget. Alternativene du returnerer skal komme HERFRA — "
        f"det er konkrete retninger deltakerne allerede har formulert:\n"
        + ("\n".join(f"- {s}" for s in raw_journey) if raw_journey else "(ingen)")
    )

    parts.append(
        f"# RESSURS-KONTEKST — Styrker deltakerne tar med seg (slide 6)\n"
        f"Dette er ting deltakerne sier de VIL BEVARE og bygge videre på. Alternativene dine bør "
        f"gjenkjenne og LØFTE disse ressursene der det passer. Ikke foreslå noe som forkaster det "
        f"deltakerne allerede sier er verdifullt.\n"
        + ("\n".join(f"- {s}" for s in raw_carry) if raw_carry else "(ingen)")
    )

    parts.append(
        f"# VARSEL-KONTEKST — Bekymringer deltakerne vil unngå (slide 7)\n"
        f"Dette er IKKE forslag til alternativer. Dette er ting deltakerne er BEKYMRET for. "
        f"Bruk listen som sjekkpunkt: alternativene dine MÅ IKKE bryte med disse bekymringene. "
        f"Hvis et alternativ fra reisen ville forsterket en av disse bekymringene, velg et annet alternativ "
        f"som fanger samme retning uten å aktivere bekymringen.\n"
        + ("\n".join(f"- {s}" for s in raw_mok) if raw_mok else "(ingen)")
    )

    # FORMAT
    parts.append(
        f"# OUTPUT\n"
        f"Returner KUN en JSON-array med {min_n}-{max_n} strenger. Ingen forklaring, ingen markdown, ingen kommentarer.\n"
        f"Eksempel: [\"Tverrfaglig vurderingsteam ved første kontakt\", \"Digital førstelinje før menneskelig kontakt\", ...]"
    )

    return "\n\n".join(parts)


def gemini_dedupe_options(raw_journey: List[str], raw_mok: List[str], raw_carry: List[str] = None, min_n: int = 6, max_n: int = 8):
    """Send kontekstualisert prompt til Gemini. Returner (options, prompt) tuple."""
    raw_carry = raw_carry or []
    if not gemini_client or (not raw_journey and not raw_mok and not raw_carry):
        return [], None
    prompt = build_dotvote_prompt(raw_journey, raw_mok, raw_carry, min_n, max_n)
    try:
        from google.genai import types
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )
        text = resp.text.strip()
        # Robust JSON-parsing
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            options = [str(s).strip() for s in parsed if str(s).strip()][:max_n]
            return options, prompt
    except Exception as e:
        print(f"[Gemini] dedupe feilet: {e}")
    return [], prompt


def fallback_dedupe(raw: List[str], max_n: int = 8) -> List[str]:
    """Enkel string-basert deduplisering når Gemini ikke er tilgjengelig."""
    seen_lower = set()
    out = []
    for s in raw:
        key = s.lower().strip()
        if key and key not in seen_lower:
            seen_lower.add(key)
            out.append(s.strip())
        if len(out) >= max_n:
            break
    return out


@app.post("/api/round/{round_id}/auto-populate")
async def auto_populate(round_id: str):
    """Hent automatisk forslag fra journey-runden + mok, dedupliser med Gemini, og sett som options."""
    if round_id not in STATE["rounds"]:
        return JSONResponse({"error": "ukjent runde"}, status_code=404)
    r = STATE["rounds"][round_id]
    if r.get("type") != "vote":
        return JSONResponse({"error": "auto-populate fungerer kun for vote-runder"}, status_code=400)

    # Samle rå input separat (Gemini får dem som forskjellige kategorier)
    raw_journey = collect_journey_steps_text()
    raw_mok = collect_round_items_text("mok")
    raw_carry = collect_round_items_text("carry")
    combined_count = len(raw_journey) + len(raw_mok) + len(raw_carry)

    if combined_count == 0:
        return JSONResponse({"error": "ingen input å hente fra", "ai_used": False}, status_code=200)

    # Forsøk Gemini først (med rik kontekst)
    options_text, prompt_used = gemini_dedupe_options(raw_journey, raw_mok, raw_carry)
    ai_used = bool(options_text)

    # Fallback hvis Gemini feiler eller er av
    if not options_text:
        options_text = fallback_dedupe(raw_journey + raw_carry + raw_mok)

    if not options_text:
        return JSONResponse({"error": "kunne ikke generere alternativer", "ai_used": False}, status_code=500)

    # Lagre som options
    r["options"] = [{"id": f"o{i}", "text": t} for i, t in enumerate(options_text)]
    # Nullstill stemmer ved auto-populate
    r["votes"] = {}
    save_state()
    await manager.broadcast({"type": "state", "state": STATE})

    return {
        "ok": True,
        "ai_used": ai_used,
        "raw_count": combined_count,
        "raw_journey_count": len(raw_journey),
        "raw_mok_count": len(raw_mok),
        "option_count": len(options_text),
        "options": [o["text"] for o in r["options"]],
        "prompt_preview": (prompt_used[:500] + "...") if prompt_used and len(prompt_used) > 500 else prompt_used,
    }


@app.get("/api/context")
async def get_context():
    """Returner gjeldende workshop-kontekst (for debug og admin-visning)."""
    return JSONResponse(WORKSHOP_CONTEXT)


def build_sentence_prompt() -> str:
    """Bygg en rik prompt for å lage 'Mestringstorget i Nordre Follo er...' basert på all workshop-data."""
    ctx = WORKSHOP_CONTEXT
    parts = []

    # Rolle + domene
    parts.append(
        f"# ROLLE\nDu fasiliterer en workshop for ledergruppen i Helse og mestring i Nordre Follo kommune. "
        f"Du skal nå hjelpe dem med å formulere én felles setning som oppsummerer hva mestringstorget SKAL VÆRE, "
        f"basert på alt de har kommet frem til i workshopen."
    )

    dom = ctx.get("domene", {})
    if dom:
        parts.append(
            f"# DOMENE\n{dom.get('navn', '')}: {dom.get('definisjon', '')}\n\n"
            f"Ambisjon: {dom.get('ambisjon', '')}"
        )

    # Nasjonal kontekst
    nk = ctx.get("nasjonal_kontekst", {}).get("helseministeren_10_april_2026", {})
    if nk:
        parts.append(
            f"# NASJONAL KONTEKST\n"
            f"{nk.get('sitat_hovedmål', '')} — Helseminister Vestre, {nk.get('kilde', '')}.\n"
            f"{nk.get('betydning', '')}"
        )

    # Oppgave og regler
    sp = ctx.get("en_setning_prompt", {})
    if sp:
        parts.append(f"# DIN OPPGAVE\n{sp.get('oppgave', '')}")
        parts.append(f"# FORMAT\n{sp.get('format', '')}")
        if sp.get("regler"):
            parts.append("# REGLER\n" + "\n".join(f"- {r}" for r in sp.get("regler", [])))

    # Alle innspill fra workshopen
    parts.append("# INNSPILL FRA WORKSHOPEN")

    # Hva tar vi med oss (styrker)
    carry = collect_round_items_text("carry")
    if carry:
        parts.append("## Styrker deltakerne tar med seg (slide 6):\n" + "\n".join(f"- {s}" for s in carry))

    # Hva vi vil unngå (bekymringer)
    mok = collect_round_items_text("mok")
    if mok:
        parts.append("## Bekymringer deltakerne vil unngå (slide 7):\n" + "\n".join(f"- {s}" for s in mok))

    # Karis nye reise
    journeys = STATE["rounds"].get("journey", {}).get("journeys", {})
    if journeys:
        parts.append("## Karis nye reise, designet av deltakerne (slide 10):")
        for uid, j in journeys.items():
            name = j.get("user_name", "Anonym")
            steps = " → ".join(j.get("steps", []))
            parts.append(f"- {name}: Kontakter mestringstorget → {steps} → Mestrer hverdagen")

    # Dot-voting top 3
    dotvote = STATE["rounds"].get("dotvote", {})
    options = dotvote.get("options", [])
    votes = dotvote.get("votes", {})
    if options and votes:
        counts = {o["id"]: 0 for o in options}
        for arr in votes.values():
            for oid in arr:
                if oid in counts:
                    counts[oid] += 1
        ranked = sorted(options, key=lambda o: counts[o["id"]], reverse=True)
        top3 = [f"{o['text']} ({counts[o['id']]} stemmer)" for o in ranked[:3] if counts[o["id"]] > 0]
        if top3:
            parts.append("## Top 3 prioritert av ledergruppen (slide 12):\n" + "\n".join(f"- {s}" for s in top3))

    # Tre retningsvalg
    for rid, label in [("who", "Hvem sitter i mestringstorget (slide 15)"),
                       ("replace", "Hva erstatter vi (slide 16)"),
                       ("format", "Fysisk/digitalt format (slide 17)")]:
        items = collect_round_items_text(rid)
        if items:
            parts.append(f"## Retningsvalg — {label}:\n" + "\n".join(f"- {s}" for s in items))

    # Forpliktelsen
    commit_items = collect_round_items_text("commit")
    if commit_items:
        parts.append("## Forpliktelsen — det som MÅ skje (slide 19):\n" + "\n".join(f"- {s}" for s in commit_items))

    parts.append(
        "# OUTPUT\n"
        "Returner KUN selve setningen som ren tekst. Ingen forklaring, ingen anførselstegn, ingen markdown. "
        "Hvis det ikke er nok input til å lage en meningsfull setning, returner teksten: "
        "INGEN_INPUT_ENNA"
    )

    return "\n\n".join(parts)


def gemini_generate_sentence():
    """Kall Gemini for å generere 'Mestringstorget i Nordre Follo er...' setningen."""
    if not gemini_client:
        return None, None
    prompt = build_sentence_prompt()
    try:
        from google.genai import types
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.5,  # litt høyere for kreativitet
            ),
        )
        text = (resp.text or "").strip()
        # Rydd bort evt anførselstegn og markdown
        text = text.strip('"').strip("'").strip('«').strip('»').strip()
        if text.startswith("```"):
            text = text.split("```")[1].strip()
            if text.startswith("text"):
                text = text[4:].strip()
        if text == "INGEN_INPUT_ENNA" or not text:
            return None, prompt
        return text, prompt
    except Exception as e:
        print(f"[Gemini] setning feilet: {e}")
        return None, prompt


@app.post("/api/generate-sentence")
async def api_generate_sentence():
    """Generer 'Mestringstorget i Nordre Follo er...' setningen basert på all workshop-data."""
    if not gemini_client:
        return JSONResponse({
            "error": "Gemini er ikke konfigurert — sett GEMINI_API_KEY på Render",
            "ai_used": False
        }, status_code=200)

    sentence, prompt = gemini_generate_sentence()
    if not sentence:
        return JSONResponse({
            "error": "Kunne ikke generere setning — for lite input fra workshopen?",
            "ai_used": False
        }, status_code=200)

    return {
        "ok": True,
        "ai_used": True,
        "sentence": sentence,
        "prompt_length": len(prompt) if prompt else 0,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send full state at connect
        await ws.send_json({"type": "init", "state": STATE})
        while True:
            data = await ws.receive_json()
            await handle_message(data, ws)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        print(f"WS error: {e}")
        manager.disconnect(ws)


async def handle_message(data: dict, ws: WebSocket):
    msg_type = data.get("type")

    if msg_type == "join":
        user_id = data.get("user_id")
        name = data.get("name", "")
        if user_id:
            STATE["participants"][user_id] = name
            save_state()
            await manager.broadcast({"type": "participants_updated", "participants": STATE["participants"]})

    elif msg_type == "submit":
        round_id = data.get("round_id")
        if round_id not in STATE["rounds"]:
            return
        r = STATE["rounds"][round_id]
        if r["type"] in ("freetext", "categorized"):
            item = {
                "id": f"{round_id}_{len(r['items'])}_{datetime.now().timestamp()}",
                "user_id": data.get("user_id", "anon"),
                "user_name": STATE["participants"].get(data.get("user_id", ""), "Anonym"),
                "value": data.get("value", "").strip(),
                "category": data.get("category"),  # for categorized
                "source_id": data.get("source_id"),  # peker tilbake til originallapp hvis sortering
                "cluster": None,
                "ts": datetime.now().isoformat(),
            }
            if item["value"]:
                r["items"].append(item)
                save_state()
                await manager.broadcast({"type": "item_added", "round_id": round_id, "item": item})

    elif msg_type == "vote":
        round_id = data.get("round_id")
        if round_id not in STATE["rounds"]:
            return
        r = STATE["rounds"][round_id]
        if r["type"] == "vote":
            user_id = data.get("user_id", "anon")
            r["votes"][user_id] = data.get("options", [])[:3]  # max 3 votes
            save_state()
            await manager.broadcast({"type": "vote_updated", "round_id": round_id, "votes": r["votes"]})

    elif msg_type == "rate":
        round_id = data.get("round_id")
        if round_id not in STATE["rounds"]:
            return
        r = STATE["rounds"][round_id]
        if r["type"] == "rating":
            user_id = data.get("user_id", "anon")
            value = int(data.get("value", 3))
            comment = data.get("comment", "")
            r["ratings"] = [x for x in r["ratings"] if x["user_id"] != user_id]
            r["ratings"].append({
                "user_id": user_id,
                "user_name": STATE["participants"].get(user_id, "Anonym"),
                "value": value,
                "comment": comment,
                "ts": datetime.now().isoformat(),
            })
            save_state()
            await manager.broadcast({"type": "rating_updated", "round_id": round_id, "ratings": r["ratings"]})

    elif msg_type == "delete_item":
        round_id = data.get("round_id")
        item_id = data.get("item_id")
        if round_id in STATE["rounds"]:
            r = STATE["rounds"][round_id]
            if "items" in r:
                r["items"] = [i for i in r["items"] if i["id"] != item_id]
                save_state()
                await manager.broadcast({"type": "item_removed", "round_id": round_id, "item_id": item_id})

    elif msg_type == "cluster_item":
        # Fasilitator grupperer en lapp
        round_id = data.get("round_id")
        item_id = data.get("item_id")
        cluster = data.get("cluster")
        if round_id in STATE["rounds"]:
            for it in STATE["rounds"][round_id].get("items", []):
                if it["id"] == item_id:
                    it["cluster"] = cluster
                    save_state()
                    await manager.broadcast({"type": "state", "state": STATE})
                    break

    elif msg_type == "set_options":
        # Fasilitator setter dot-voting opsjoner
        round_id = data.get("round_id")
        if round_id in STATE["rounds"]:
            STATE["rounds"][round_id]["options"] = data.get("options", [])
            save_state()
            await manager.broadcast({"type": "state", "state": STATE})

    elif msg_type == "submit_journey":
        # Deltaker sender inn sin komplette reise (liste med steg)
        round_id = data.get("round_id")
        if round_id not in STATE["rounds"]:
            return
        r = STATE["rounds"][round_id]
        if r.get("type") == "journey":
            user_id = data.get("user_id", "anon")
            steps = [s.strip() for s in data.get("steps", []) if s and s.strip()]
            if not steps:
                return
            r.setdefault("journeys", {})
            r["journeys"][user_id] = {
                "steps": steps,
                "user_name": STATE["participants"].get(user_id, "Anonym"),
                "ts": datetime.now().isoformat(),
            }
            save_state()
            await manager.broadcast({
                "type": "journey_updated",
                "round_id": round_id,
                "user_id": user_id,
                "journey": r["journeys"][user_id],
            })

    elif msg_type == "delete_journey":
        # Fasilitator (eller deltaker selv) sletter en journey
        round_id = data.get("round_id")
        user_id = data.get("user_id")
        if round_id in STATE["rounds"]:
            r = STATE["rounds"][round_id]
            if "journeys" in r and user_id in r["journeys"]:
                del r["journeys"][user_id]
                save_state()
                await manager.broadcast({
                    "type": "journey_removed",
                    "round_id": round_id,
                    "user_id": user_id,
                })


# ---------- HTML TEMPLATES ----------

LANDING_HTML = """<!DOCTYPE html>
<html lang="no"><head><meta charset="UTF-8"><title>Mestringstorget Workshop Server</title>
<style>
body{font-family:Inter,sans-serif;background:#1a1a2e;color:#fff;margin:0;padding:60px 40px;text-align:center}
h1{font-size:42px;color:#00a896;margin-bottom:8px}
p{color:#aaa;font-size:18px}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;max-width:1100px;margin:50px auto}
.card{background:#16213e;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:30px;text-decoration:none;color:#fff;transition:transform .2s,border-color .2s}
.card:hover{transform:translateY(-4px);border-color:#00a896}
.card h2{color:#02c8a7;margin:0 0 10px;font-size:22px}
.card p{margin:0;font-size:14px;color:#aaa}
.ip{background:#0f3460;padding:20px;border-radius:12px;display:inline-block;margin-top:30px;font-family:monospace;font-size:18px}
.ip strong{color:#02c8a7}
</style></head><body>
<h1>Mestringstorget Workshop Server</h1>
<p>Live digital input for ledergruppen Helse og mestring</p>

<div class="ip">Server: <strong>http://{{IP}}:8000</strong></div>

<div class="cards">
  <a class="card" href="/admin"><h2>&rarr; Fasilitator</h2><p>Styr runder, se alt, eksporter</p></a>
  <a class="card" href="/wall"><h2>&rarr; Live Wall</h2><p>Vises pa storskjerm i workshopen</p></a>
  <a class="card" href="/p"><h2>&rarr; Deltaker</h2><p>Test deltaker-input</p></a>
</div>

<div style="margin-top:40px">
  <p style="font-size:14px">Deltakerne apner: <strong style="color:#02c8a7">http://{{IP}}:8000/p</strong></p>
  <p style="font-size:14px">Live wall: <strong style="color:#02c8a7">http://{{IP}}:8000/wall</strong></p>
</div>
</body></html>"""


PARTICIPANT_HTML = """<!DOCTYPE html>
<html lang="no"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workshop — Mestringstorget</title>
<style>
:root { --teal:#00a896; --teal-l:#02c8a7; --dark:#1a1a2e; --blue:#16213e; --warm:#e8634a; --gold:#f5a623; --green:#22c55e; --purple:#7b61ff; }
* { box-sizing: border-box; margin:0; padding:0; }
body { font-family: 'Inter', -apple-system, sans-serif; background: linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); color:#fff; min-height:100vh; padding:20px; }
.container { max-width: 720px; margin: 0 auto; }
.header { text-align:center; margin-bottom:24px; }
.header h1 { font-size:24px; color:var(--teal-l); }
.header .status { font-size:12px; color:#888; margin-top:6px; }
.status .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; margin-right:6px; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.name-input { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.15); border-radius:12px; padding:14px 18px; color:#fff; font-size:16px; width:100%; margin-bottom:20px; }
.card { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.1); border-radius:18px; padding:24px; margin-bottom:18px; }
.card.active { border-color:var(--teal); box-shadow: 0 0 30px rgba(0,168,150,.2); }
.label { font-size:11px; color:var(--teal); letter-spacing:2px; text-transform:uppercase; font-weight:700; }
.title { font-size:22px; font-weight:800; margin-top:8px; }
.question { font-size:17px; color:#ccc; margin-top:14px; line-height:1.5; }
.input-area { margin-top:20px; }
textarea { width:100%; background:rgba(0,0,0,.3); border:1px solid rgba(255,255,255,.15); border-radius:12px; padding:14px 16px; color:#fff; font-size:16px; resize:vertical; min-height:80px; font-family:inherit; }
textarea:focus { outline:none; border-color:var(--teal); }
button { background:var(--teal); color:#fff; border:none; border-radius:12px; padding:14px 28px; font-size:16px; font-weight:700; cursor:pointer; width:100%; margin-top:12px; transition: background .2s, transform .1s; }
button:hover { background:var(--teal-l); }
button:active { transform: scale(.97); }
button:disabled { opacity:.5; cursor:not-allowed; }
.cat-buttons { display:flex; gap:8px; margin:12px 0; }
.cat-buttons button { flex:1; margin:0; padding:10px; font-size:13px; }
.cat-Behold { background:var(--green); }
.cat-Endre { background:var(--gold); }
.cat-Slipp { background:#ef4444; }
.my-items { margin-top:16px; }
.my-item { background:rgba(0,0,0,.25); border-left:3px solid var(--teal); padding:10px 14px; margin-bottom:8px; border-radius:8px; font-size:14px; display:flex; justify-content:space-between; align-items:center; }
.my-item button { width:auto; padding:4px 10px; margin:0; font-size:12px; background:#444; }
.my-item button:hover { background:#ef4444; }
.empty { text-align:center; padding:60px 20px; color:#666; font-size:16px; }
.vote-options { display:flex; flex-direction:column; gap:8px; margin-top:16px; }
.vote-option { background:rgba(0,0,0,.3); border:2px solid rgba(255,255,255,.1); border-radius:10px; padding:14px; cursor:pointer; transition: all .2s; }
.vote-option.selected { border-color:var(--teal); background:rgba(0,168,150,.2); }
.vote-option .badge { float:right; background:var(--teal); color:#fff; border-radius:999px; padding:2px 10px; font-size:12px; font-weight:700; }
.vote-status { text-align:center; color:#aaa; font-size:14px; margin-top:8px; }
.rating-row { display:flex; justify-content:space-between; gap:6px; margin:14px 0; }
.rating-btn { flex:1; padding:18px 0; border-radius:12px; border:2px solid rgba(255,255,255,.15); background:rgba(0,0,0,.3); color:#fff; font-size:24px; font-weight:800; cursor:pointer; transition: all .2s; }
.rating-btn:hover { border-color:var(--teal); }
.rating-btn.selected { background:var(--teal); border-color:var(--teal-l); transform: scale(1.1); }
.rating-labels { display:flex; justify-content:space-between; font-size:11px; color:#888; }

/* Rating-kontekst (viser hva du stemmer på) */
.rating-context { background:rgba(0,168,150,.08); border:1px solid rgba(0,168,150,.25); border-radius:12px; padding:14px 16px; margin-top:12px; }
.rating-context .ctx-label { font-size:11px; color:var(--teal-l); letter-spacing:1.5px; text-transform:uppercase; font-weight:700; margin-bottom:6px; }
.rating-context .ctx-title { font-size:15px; color:#fff; font-weight:600; margin-bottom:10px; line-height:1.3; }
.rating-context .ctx-items { display:flex; flex-direction:column; gap:6px; max-height:240px; overflow-y:auto; padding-right:4px; }
.rating-context .ctx-item { background:rgba(255,255,255,.06); border-left:2px solid var(--teal); padding:7px 10px; border-radius:6px; font-size:13px; color:rgba(255,255,255,.85); line-height:1.35; }
.rating-context .ctx-item.ctx-more { border-left-color:rgba(255,255,255,.2); font-style:italic; color:rgba(255,255,255,.5); text-align:center; }
.rating-confirmed { background:rgba(34,197,94,.15); border:1px solid rgba(34,197,94,.4); color:#86efac; padding:10px 14px; border-radius:10px; font-size:13px; margin-top:12px; text-align:center; }

/* Sorteringskort for categorized-runden (BEHOLD/ENDRE/SLIPP) */
.sort-item { background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.1); border-radius:10px; padding:12px; margin-bottom:8px; }
.sort-item .sort-text { font-size:14px; color:#fff; font-weight:500; }
.sort-item .sort-author { font-size:11px; color:#888; margin-top:4px; font-style:italic; }
.sort-item .sort-buttons { display:flex; gap:6px; margin-top:10px; }
.sort-item .sort-buttons button { flex:1; padding:8px 0; font-size:12px; margin:0; }

/* Journey builder */
.journey-builder { margin-top:14px; }
.j-step { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.j-step .num { background:var(--teal); color:#fff; width:32px; height:32px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:14px; flex-shrink:0; }
.j-step input { flex:1; background:rgba(0,0,0,.3); border:1px solid rgba(255,255,255,.15); border-radius:10px; padding:12px 14px; color:#fff; font-size:15px; font-family:inherit; }
.j-step input:focus { outline:none; border-color:var(--teal); }
.j-step .remove { background:rgba(239,68,68,.2); color:#fca5a5; border:1px solid rgba(239,68,68,.3); width:32px; height:32px; border-radius:50%; cursor:pointer; flex-shrink:0; font-size:18px; padding:0; margin:0; line-height:1; }
.j-step .remove:hover { background:rgba(239,68,68,.5); color:#fff; }
.j-step.fixed { opacity:.6; }
.j-step.fixed input { background:rgba(0,168,150,.1); border-color:rgba(0,168,150,.3); pointer-events:none; }
.j-add { background:rgba(255,255,255,.05); border:1px dashed rgba(255,255,255,.2); color:#888; padding:12px; border-radius:10px; cursor:pointer; width:100%; margin-top:4px; font-size:13px; }
.j-add:hover { background:rgba(0,168,150,.1); border-color:var(--teal); color:var(--teal-l); }
.j-saved-banner { background:rgba(34,197,94,.15); border:1px solid rgba(34,197,94,.4); color:#86efac; padding:10px 14px; border-radius:10px; font-size:13px; margin-bottom:12px; text-align:center; }
</style></head>
<body>
<div class="container">
  <div class="header">
    <h1>Mestringstorget Workshop</h1>
    <div class="status"><span class="dot"></span><span id="status">Kobler til...</span></div>
  </div>

  <input class="name-input" id="name" placeholder="Ditt navn (valgfritt)" />

  <div id="content">
    <div class="card empty">Venter pa fasilitator a starte en runde...</div>
  </div>
</div>

<script>
const userId = localStorage.getItem('uid') || 'u_' + Math.random().toString(36).slice(2,10);
localStorage.setItem('uid', userId);
const nameInput = document.getElementById('name');
nameInput.value = localStorage.getItem('name') || '';
nameInput.addEventListener('change', () => {
  localStorage.setItem('name', nameInput.value);
  ws.send(JSON.stringify({type:'join', user_id:userId, name:nameInput.value}));
});

let state = null;
let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    document.getElementById('status').textContent = 'Tilkoblet';
    ws.send(JSON.stringify({type:'join', user_id:userId, name:nameInput.value}));
  };
  ws.onclose = () => {
    document.getElementById('status').textContent = 'Frakoblet — kobler til igjen...';
    setTimeout(connect, 1500);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'init' || msg.state) state = msg.state;
    else if (msg.type === 'round_changed') state = msg.state;
    else if (msg.type === 'item_added') {
      if (state) state.rounds[msg.round_id].items.push(msg.item);
    } else if (msg.type === 'item_removed') {
      if (state) state.rounds[msg.round_id].items = state.rounds[msg.round_id].items.filter(i => i.id !== msg.item_id);
    } else if (msg.type === 'vote_updated') {
      if (state) state.rounds[msg.round_id].votes = msg.votes;
    } else if (msg.type === 'journey_updated') {
      if (state) {
        const r = state.rounds[msg.round_id];
        if (r) { r.journeys = r.journeys || {}; r.journeys[msg.user_id] = msg.journey; }
      }
    } else if (msg.type === 'journey_removed') {
      if (state) {
        const r = state.rounds[msg.round_id];
        if (r && r.journeys) delete r.journeys[msg.user_id];
      }
    }
    render();
  };
}

function activeRound() {
  if (!state || !state.active_round) return null;
  return [state.active_round, state.rounds[state.active_round]];
}

function myItems(roundId) {
  if (!state) return [];
  const r = state.rounds[roundId];
  return (r.items || []).filter(i => i.user_id === userId);
}

function submitItem(roundId, value, category=null) {
  ws.send(JSON.stringify({type:'submit', round_id:roundId, user_id:userId, value, category}));
}

function deleteItem(roundId, itemId) {
  ws.send(JSON.stringify({type:'delete_item', round_id:roundId, item_id:itemId}));
}

function vote(roundId, optionId) {
  const r = state.rounds[roundId];
  let mine = (r.votes && r.votes[userId]) || [];
  if (mine.includes(optionId)) {
    mine = mine.filter(o => o !== optionId);
  } else if (mine.length < 3) {
    mine = [...mine, optionId];
  }
  ws.send(JSON.stringify({type:'vote', round_id:roundId, user_id:userId, options:mine}));
}

function rate(roundId, value) {
  const comment = document.getElementById('rate-comment')?.value || '';
  ws.send(JSON.stringify({type:'rate', round_id:roundId, user_id:userId, value, comment}));
}

function render() {
  const content = document.getElementById('content');
  const ar = activeRound();
  if (!ar) {
    content.innerHTML = '<div class="card empty">Venter pa fasilitator a starte en runde...</div>';
    return;
  }
  const [rid, r] = ar;

  if (r.type === 'freetext') {
    const mine = myItems(rid);
    content.innerHTML = `
      <div class="card active">
        <div class="label">Aktiv runde</div>
        <div class="title">${r.title}</div>
        <div class="question">${r.question}</div>
        <div class="input-area">
          <textarea id="ta" placeholder="Skriv din lapp her..."></textarea>
          <button onclick="sendOne()">Send inn lapp</button>
        </div>
        <div class="my-items">
          <div style="font-size:12px;color:#888;margin:14px 0 6px;letter-spacing:1px">DINE LAPPER (${mine.length})</div>
          ${mine.map(i => `<div class="my-item"><span>${escape(i.value)}</span><button onclick="deleteItem('${rid}','${i.id}')">Slett</button></div>`).join('')}
          ${mine.length === 0 ? '<div style="font-size:13px;color:#666;text-align:center;padding:14px">Ingen lapper enna</div>' : ''}
        </div>
      </div>`;
  } else if (r.type === 'categorized') {
    const mine = myItems(rid);

    // Hent lapper fra mok-runden som ikke allerede er sortert inn i denne runden
    const mokItems = (state.rounds.mok && state.rounds.mok.items) || [];
    const alreadySorted = new Set((r.items || []).map(i => i.source_id).filter(Boolean));
    const toSort = mokItems.filter(it => !alreadySorted.has(it.id));

    content.innerHTML = `
      <div class="card active">
        <div class="label">Aktiv runde — sortering</div>
        <div class="title">${r.title}</div>
        <div class="question">${r.question}</div>

        ${toSort.length > 0 ? `
          <div style="margin-top:14px;background:rgba(0,0,0,.25);border-radius:10px;padding:12px;">
            <div style="font-size:11px;color:var(--teal-l);letter-spacing:1.5px;text-transform:uppercase;font-weight:700;margin-bottom:8px;">Lapper fra forrige runde &mdash; klikk for å sortere</div>
            <div id="sort-queue">
              ${toSort.map(it => `
                <div class="sort-item" data-source-id="${it.id}">
                  <div class="sort-text">${escape(it.value)}</div>
                  <div class="sort-author">${escape(it.user_name || 'Anonym')}</div>
                  <div class="sort-buttons">
                    ${r.categories.map(c => `<button class="cat-${c}" onclick="sortItem('${it.id}', ${JSON.stringify(it.value).replace(/"/g,'&quot;')}, '${c}')">${c}</button>`).join('')}
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}

        <div class="input-area" style="margin-top:14px;">
          <div style="font-size:13px;color:#888;margin-bottom:8px;">Eller skriv ny lapp:</div>
          <textarea id="ta" placeholder="Ny lapp her..."></textarea>
          <div class="cat-buttons">
            ${r.categories.map(c => `<button class="cat-${c}" onclick="sendCat('${c}')">${c}</button>`).join('')}
          </div>
        </div>

        <div class="my-items">
          ${mine.length > 0 ? `<div style="font-size:11px;color:#888;letter-spacing:1.5px;text-transform:uppercase;margin-top:14px;margin-bottom:6px;">Sortert av deg (${mine.length})</div>` : ''}
          ${mine.map(i => `<div class="my-item"><span><strong style="color:var(--teal-l)">[${i.category}]</strong> ${escape(i.value)}</span><button onclick="deleteItem('${rid}','${i.id}')">Slett</button></div>`).join('')}
        </div>
      </div>`;
  } else if (r.type === 'vote') {
    const mine = (r.votes && r.votes[userId]) || [];
    content.innerHTML = `
      <div class="card active">
        <div class="label">Aktiv runde — Stem maks 3</div>
        <div class="title">${r.title}</div>
        <div class="question">${r.question}</div>
        <div class="vote-options">
          ${(r.options || []).map(opt => `
            <div class="vote-option ${mine.includes(opt.id) ? 'selected' : ''}" onclick="vote('${rid}','${opt.id}')">
              ${escape(opt.text)}
              ${mine.includes(opt.id) ? '<span class="badge">+1</span>' : ''}
            </div>
          `).join('')}
        </div>
        <div class="vote-status">${mine.length} av 3 stemmer brukt</div>
      </div>`;
  } else if (r.type === 'rating') {
    const myRating = (r.ratings || []).find(x => x.user_id === userId);
    const sel = myRating ? myRating.value : 0;

    // Hent parent-runde hvis definert, vis lapper som kontekst for hva man stemmer paa
    let parentHtml = '';
    if (r.parent && state.rounds[r.parent]) {
      const parent = state.rounds[r.parent];
      const items = parent.items || [];
      if (items.length > 0) {
        parentHtml = `
          <div class="rating-context">
            <div class="ctx-label">Du stemmer på retningen som kom frem om:</div>
            <div class="ctx-title">${escape(parent.question || parent.title || '')}</div>
            <div class="ctx-items">
              ${items.slice(0, 20).map(it => `<div class="ctx-item">${escape(it.value)}</div>`).join('')}
              ${items.length > 20 ? `<div class="ctx-item ctx-more">+ ${items.length - 20} flere</div>` : ''}
            </div>
          </div>`;
      }
    }

    content.innerHTML = `
      <div class="card active">
        <div class="label">Aktiv runde — Konsensussjekk</div>
        <div class="title">${r.title}</div>
        ${parentHtml}
        <div class="question" style="margin-top:14px;">${r.question}</div>
        <div class="rating-row">
          ${[1,2,3,4,5].map(v => `<button class="rating-btn ${sel === v ? 'selected' : ''}" onclick="rate('${rid}',${v})">${v}</button>`).join('')}
        </div>
        <div class="rating-labels">
          <span>Kan ikke leve</span><span></span><span>Kan leve med</span><span></span><span>Helt enig</span>
        </div>
        <textarea id="rate-comment" placeholder="Eventuell kommentar..." style="margin-top:14px"></textarea>
        ${myRating ? '<div class="rating-confirmed">✓ Du stemte <strong>' + sel + '</strong>. Du kan endre ved å klikke en annen.</div>' : ''}
      </div>`;
  } else if (r.type === 'journey') {
    const mine = (r.journeys || {})[userId];
    const savedSteps = mine ? mine.steps : null;
    // Lokal arbeidsstate (for ulagrede endringer) - persisteres ikke mellom render
    if (!window.journeyDraft || window.journeyDraftRound !== rid) {
      window.journeyDraft = savedSteps ? savedSteps.slice() : ['', '', ''];
      window.journeyDraftRound = rid;
    }
    const steps = window.journeyDraft;
    content.innerHTML = `
      <div class="card active">
        <div class="label">Aktiv runde — Designe reisen</div>
        <div class="title">${r.title}</div>
        <div class="question">${r.question}</div>
        ${mine ? '<div class="j-saved-banner">✓ Reisen din er sendt. Du kan endre den og sende på nytt.</div>' : ''}
        <div class="journey-builder">
          <div class="j-step fixed">
            <div class="num" style="background:var(--green);">↓</div>
            <input type="text" value="Kari kontakter mestringstorget" disabled />
          </div>
          <div id="journey-steps">
            ${steps.map((s, i) => `
              <div class="j-step">
                <div class="num">${i+1}</div>
                <input type="text" value="${escape(s)}" placeholder="Beskriv steg ${i+1}..." oninput="updateJourneyStep(${i}, this.value)" />
                <button class="remove" onclick="removeJourneyStep(${i})" title="Fjern steg">×</button>
              </div>
            `).join('')}
          </div>
          <button class="j-add" onclick="addJourneyStep()">+ Legg til nytt steg</button>
          <div class="j-step fixed" style="margin-top:10px;">
            <div class="num" style="background:var(--green);">✓</div>
            <input type="text" value="Kari mestrer hverdagen" disabled />
          </div>
        </div>
        <button onclick="sendJourney()" style="margin-top:14px;">${mine ? 'Oppdater min reise' : 'Send min reise'}</button>
      </div>`;
  }
}

function updateJourneyStep(idx, val) {
  if (!window.journeyDraft) return;
  window.journeyDraft[idx] = val;
}

function addJourneyStep() {
  if (!window.journeyDraft) window.journeyDraft = [];
  if (window.journeyDraft.length >= 8) return;
  window.journeyDraft.push('');
  render();
}

function removeJourneyStep(idx) {
  if (!window.journeyDraft || window.journeyDraft.length <= 1) return;
  window.journeyDraft.splice(idx, 1);
  render();
}

function sendJourney() {
  const ar = activeRound();
  if (!ar) return;
  const steps = (window.journeyDraft || []).map(s => (s || '').trim()).filter(Boolean);
  if (!steps.length) return;
  ws.send(JSON.stringify({type:'submit_journey', round_id:ar[0], user_id:userId, steps}));
}

function sendOne() {
  const ar = activeRound();
  if (!ar) return;
  const ta = document.getElementById('ta');
  const v = ta.value.trim();
  if (!v) return;
  submitItem(ar[0], v);
  ta.value = '';
}

function sortItem(sourceId, value, cat) {
  const ar = activeRound();
  if (!ar) return;
  ws.send(JSON.stringify({
    type:'submit', round_id:ar[0], user_id:userId,
    value: value, category: cat, source_id: sourceId
  }));
}

function sendCat(cat) {
  const ar = activeRound();
  if (!ar) return;
  const ta = document.getElementById('ta');
  const v = ta.value.trim();
  if (!v) return;
  submitItem(ar[0], v, cat);
  ta.value = '';
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

connect();
</script>
</body></html>"""


WALL_HTML = """<!DOCTYPE html>
<html lang="no"><head><meta charset="UTF-8"><title>Live Wall — Mestringstorget</title>
<style>
:root { --teal:#00a896; --teal-l:#02c8a7; --dark:#1a1a2e; --blue:#16213e; --warm:#e8634a; --gold:#f5a623; --green:#22c55e; --purple:#7b61ff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family: 'Inter', -apple-system, sans-serif; background: linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); color:#fff; min-height:100vh; padding:32px 40px; overflow-x:hidden; }
.header { display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:30px; }
.header .label { font-size:13px; color:var(--teal); letter-spacing:3px; text-transform:uppercase; font-weight:700; }
.header h1 { font-size:38px; margin-top:6px; }
.header .question { font-size:18px; color:#ccc; margin-top:4px; max-width:900px; }
.stats { text-align:right; }
.stats .num { font-size:48px; color:var(--teal-l); font-weight:800; line-height:1; }
.stats .label { font-size:13px; color:#888; }
.wall { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:18px; }
.note { background:linear-gradient(135deg,#fef3c7,#fde68a); color:#1f2937; padding:18px 20px; border-radius:8px; box-shadow:0 6px 16px rgba(0,0,0,.3); transform:rotate(0deg); animation:noteIn .5s cubic-bezier(.2,.9,.3,1.4); position:relative; min-height:90px; }
.note:nth-child(3n+1) { transform:rotate(-1deg); background:linear-gradient(135deg,#fef3c7,#fde68a); }
.note:nth-child(3n+2) { transform:rotate(1deg); background:linear-gradient(135deg,#dbeafe,#bfdbfe); }
.note:nth-child(3n) { transform:rotate(-0.5deg); background:linear-gradient(135deg,#dcfce7,#bbf7d0); }
.note .text { font-size:15px; line-height:1.4; font-weight:500; }
.note .author { font-size:11px; color:#6b7280; margin-top:10px; font-style:italic; }
.note.cat-Behold { background:linear-gradient(135deg,#dcfce7,#bbf7d0); }
.note.cat-Endre { background:linear-gradient(135deg,#fef3c7,#fde68a); }
.note.cat-Slipp { background:linear-gradient(135deg,#fee2e2,#fecaca); }
.note .cat-tag { position:absolute; top:8px; right:8px; font-size:9px; font-weight:800; letter-spacing:1px; padding:3px 8px; border-radius:4px; background:#1f2937; color:#fff; }
@keyframes noteIn { from { opacity:0; transform: translateY(20px) rotate(0deg) scale(.9); } to { opacity:1; transform: rotate(var(--rot,0deg)) scale(1); } }
.empty { text-align:center; padding:120px 20px; color:#444; font-size:24px; }
.empty span { display:block; font-size:60px; margin-bottom:20px; color:#222; }
.cat-section { margin-bottom:30px; }
.cat-section h2 { font-size:20px; margin-bottom:14px; padding-bottom:10px; border-bottom:2px solid; }
.cat-section.Behold h2 { color:var(--green); border-color:var(--green); }
.cat-section.Endre h2 { color:var(--gold); border-color:var(--gold); }
.cat-section.Slipp h2 { color:#ef4444; border-color:#ef4444; }
.vote-list { display:flex; flex-direction:column; gap:10px; max-width:900px; }
.vote-row { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.1); border-radius:10px; padding:14px 18px; display:flex; justify-content:space-between; align-items:center; transition:background .3s; }
.vote-row .text { font-size:18px; }
.vote-row .bar-container { width:300px; height:24px; background:rgba(0,0,0,.3); border-radius:6px; margin-left:14px; position:relative; overflow:hidden; }
.vote-row .bar { height:100%; background:linear-gradient(90deg,var(--teal),var(--teal-l)); transition: width .5s ease; border-radius:6px; }
.vote-row .num { position:absolute; right:10px; top:50%; transform:translateY(-50%); font-weight:800; font-size:14px; color:#fff; text-shadow:0 1px 3px rgba(0,0,0,.5); }
.rating-board { display:grid; grid-template-columns:repeat(5,1fr); gap:18px; margin-top:24px; }
.rating-col { background:rgba(255,255,255,.05); border-radius:14px; padding:24px; text-align:center; border:1px solid rgba(255,255,255,.1); }
.rating-col .num { font-size:60px; font-weight:800; line-height:1; }
.rating-col .label { font-size:12px; color:#888; margin-top:8px; }
.rating-col .count { font-size:36px; color:#fff; margin-top:14px; font-weight:700; }
.rating-col.r1 .num { color:#ef4444; }
.rating-col.r2 .num { color:#f59e0b; }
.rating-col.r3 .num { color:#9ca3af; }
.rating-col.r4 .num { color:var(--teal-l); }
.rating-col.r5 .num { color:var(--green); }
.consensus-summary { background:rgba(0,168,150,.06); border:1px solid rgba(0,168,150,.3); border-radius:14px; padding:22px; margin-top:24px; text-align:center; }
.consensus-summary .big { font-size:42px; font-weight:800; color:var(--teal-l); }
</style></head>
<body>
<div class="header">
  <div>
    <div class="label" id="round-label">Venter</div>
    <h1 id="round-title">Live Wall</h1>
    <div class="question" id="round-question">Apne /admin og start en runde</div>
  </div>
  <div class="stats">
    <div class="num" id="count">0</div>
    <div class="label" id="count-label">lapper</div>
  </div>
</div>

<div id="wall"><div class="empty"><span>&#9202;</span>Venter pa input...</div></div>

<script>
let state = null;
let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => console.log('connected');
  ws.onclose = () => setTimeout(connect, 1500);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.state) state = msg.state;
    else if (msg.type === 'item_added') {
      if (state) state.rounds[msg.round_id].items.push(msg.item);
    } else if (msg.type === 'item_removed') {
      if (state) state.rounds[msg.round_id].items = state.rounds[msg.round_id].items.filter(i => i.id !== msg.item_id);
    } else if (msg.type === 'vote_updated') {
      if (state) state.rounds[msg.round_id].votes = msg.votes;
    } else if (msg.type === 'rating_updated') {
      if (state) state.rounds[msg.round_id].ratings = msg.ratings;
    } else if (msg.type === 'journey_updated') {
      if (state) {
        const r = state.rounds[msg.round_id];
        if (r) { r.journeys = r.journeys || {}; r.journeys[msg.user_id] = msg.journey; }
      }
    } else if (msg.type === 'journey_removed') {
      if (state) {
        const r = state.rounds[msg.round_id];
        if (r && r.journeys) delete r.journeys[msg.user_id];
      }
    }
    render();
  };
}

function render() {
  if (!state || !state.active_round) {
    document.getElementById('round-label').textContent = 'Venter';
    document.getElementById('round-title').textContent = 'Live Wall';
    document.getElementById('round-question').textContent = 'Apne /admin og start en runde';
    document.getElementById('wall').innerHTML = '<div class="empty"><span>&#9202;</span>Venter pa input...</div>';
    document.getElementById('count').textContent = '0';
    return;
  }
  const rid = state.active_round;
  const r = state.rounds[rid];
  document.getElementById('round-label').textContent = 'Aktiv runde';
  document.getElementById('round-title').textContent = r.title;
  document.getElementById('round-question').textContent = r.question;

  const wall = document.getElementById('wall');

  if (r.type === 'freetext') {
    const items = r.items || [];
    document.getElementById('count').textContent = items.length;
    document.getElementById('count-label').textContent = items.length === 1 ? 'lapp' : 'lapper';
    if (items.length === 0) {
      wall.innerHTML = '<div class="empty"><span>&#9203;</span>Ingen lapper enna...</div>';
    } else {
      wall.className = 'wall';
      wall.innerHTML = items.map(i => `
        <div class="note">
          <div class="text">${escape(i.value)}</div>
          <div class="author">${escape(i.user_name || 'Anonym')}</div>
        </div>
      `).join('');
    }
  } else if (r.type === 'categorized') {
    const items = r.items || [];
    document.getElementById('count').textContent = items.length;
    document.getElementById('count-label').textContent = 'lapper';
    wall.className = '';
    wall.innerHTML = r.categories.map(cat => {
      const catItems = items.filter(i => i.category === cat);
      return `
        <div class="cat-section ${cat}">
          <h2>${cat} (${catItems.length})</h2>
          <div class="wall">
            ${catItems.map(i => `<div class="note cat-${cat}"><span class="cat-tag">${cat}</span><div class="text">${escape(i.value)}</div><div class="author">${escape(i.user_name)}</div></div>`).join('')}
          </div>
        </div>`;
    }).join('');
  } else if (r.type === 'vote') {
    const totalVotes = Object.values(r.votes || {}).reduce((s, arr) => s + arr.length, 0);
    document.getElementById('count').textContent = totalVotes;
    document.getElementById('count-label').textContent = 'stemmer';
    const counts = {};
    (r.options || []).forEach(o => counts[o.id] = 0);
    Object.values(r.votes || {}).forEach(arr => arr.forEach(oid => { if (counts[oid] !== undefined) counts[oid]++; }));
    const max = Math.max(1, ...Object.values(counts));
    const sorted = (r.options || []).slice().sort((a, b) => counts[b.id] - counts[a.id]);
    wall.className = '';
    wall.innerHTML = `<div class="vote-list">${sorted.map(opt => `
      <div class="vote-row">
        <span class="text">${escape(opt.text)}</span>
        <div class="bar-container"><div class="bar" style="width:${(counts[opt.id]/max)*100}%"></div><div class="num">${counts[opt.id]}</div></div>
      </div>
    `).join('')}</div>`;
  } else if (r.type === 'rating') {
    const ratings = r.ratings || [];
    document.getElementById('count').textContent = ratings.length;
    document.getElementById('count-label').textContent = 'stemmer';
    const buckets = [0,0,0,0,0,0]; // index 1-5
    ratings.forEach(rt => buckets[rt.value]++);
    const avg = ratings.length ? (ratings.reduce((s,r) => s+r.value, 0) / ratings.length).toFixed(1) : '—';
    wall.className = '';
    wall.innerHTML = `
      <div class="rating-board">
        ${[1,2,3,4,5].map(v => `<div class="rating-col r${v}"><div class="num">${v}</div><div class="label">${['Kan ikke leve','Sterke innvendinger','Kan leve med','God lasning','Helt enig'][v-1]}</div><div class="count">${buckets[v]}</div></div>`).join('')}
      </div>
      <div class="consensus-summary">
        Snitt: <span class="big">${avg}</span><br>
        ${ratings.length} av ${Object.keys(state.participants).length} har stemt
        ${ratings.filter(rt => rt.value < 3).length > 0 ? `<div style="color:#ef4444;margin-top:10px">${ratings.filter(rt => rt.value < 3).length} har sterke innvendinger</div>` : ''}
      </div>`;
  } else if (r.type === 'journey') {
    const journeys = Object.entries(r.journeys || {});
    document.getElementById('count').textContent = journeys.length;
    document.getElementById('count-label').textContent = 'reiser';
    wall.className = '';
    if (!journeys.length) {
      wall.innerHTML = '<div class="empty"><span>&#128679;</span>Venter på første reise...</div>';
    } else {
      wall.innerHTML = '<div style="display:flex;flex-direction:column;gap:18px;">' + journeys.map(([uid, j]) => {
        const stepsHtml = j.steps.map((s, i) => `<div style="background:linear-gradient(135deg,#dbeafe,#bfdbfe);color:#1f2937;padding:12px 16px;border-radius:8px;font-size:14px;font-weight:500;flex:1;min-width:140px;box-shadow:0 4px 10px rgba(0,0,0,.3);text-align:center;">${escape(s)}</div>`).join('<div style="font-size:24px;color:rgba(255,255,255,.4);align-self:center;">→</div>');
        return `<div style="background:rgba(255,255,255,.04);padding:18px;border-radius:14px;border:1px solid rgba(255,255,255,.08);">
          <div style="font-size:14px;color:#02c8a7;font-weight:700;margin-bottom:12px;letter-spacing:1px;text-transform:uppercase;">${escape(j.user_name || 'Anonym')}</div>
          <div style="display:flex;align-items:stretch;gap:8px;flex-wrap:wrap;">
            <div style="background:linear-gradient(135deg,#dcfce7,#bbf7d0);color:#1f2937;padding:12px 16px;border-radius:8px;font-size:14px;font-weight:600;text-align:center;box-shadow:0 4px 10px rgba(0,0,0,.3);">Kontakter mestringstorget</div>
            <div style="font-size:24px;color:rgba(255,255,255,.4);align-self:center;">→</div>
            ${stepsHtml}
            <div style="font-size:24px;color:rgba(255,255,255,.4);align-self:center;">→</div>
            <div style="background:linear-gradient(135deg,#dcfce7,#bbf7d0);color:#1f2937;padding:12px 16px;border-radius:8px;font-size:14px;font-weight:600;text-align:center;box-shadow:0 4px 10px rgba(0,0,0,.3);">Mestrer hverdagen</div>
          </div>
        </div>`;
      }).join('') + '</div>';
    }
  }
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

connect();
</script>
</body></html>"""


ADMIN_HTML = """<!DOCTYPE html>
<html lang="no"><head><meta charset="UTF-8"><title>Admin — Mestringstorget Workshop</title>
<style>
* { box-sizing:border-box; margin:0; padding:0; font-family: 'Inter', -apple-system, sans-serif; }
body { background:#1a1a2e; color:#fff; padding:30px; min-height:100vh; }
.container { max-width:1400px; margin:0 auto; }
h1 { color:#02c8a7; margin-bottom:8px; }
.meta { color:#888; margin-bottom:30px; font-size:14px; }
.rounds { display:grid; grid-template-columns:repeat(3,1fr); gap:18px; margin-bottom:30px; }
.round { background:#16213e; border:1px solid rgba(255,255,255,.1); border-radius:14px; padding:20px; }
.round.active { border-color:#00a896; box-shadow:0 0 30px rgba(0,168,150,.2); }
.round h3 { font-size:15px; color:#02c8a7; margin-bottom:8px; }
.round .desc { font-size:12px; color:#888; margin-bottom:14px; min-height:30px; }
.round .count { font-size:24px; color:#fff; font-weight:800; margin-bottom:10px; }
.round button { width:100%; padding:10px; border:none; border-radius:8px; background:#0f3460; color:#fff; cursor:pointer; font-weight:700; margin-top:6px; }
.round button:hover { background:#00a896; }
.round button.active { background:#00a896; }
.round button.danger { background:#3a1a1a; }
.round button.danger:hover { background:#ef4444; }
.live { background:#16213e; border-radius:14px; padding:24px; }
.live h2 { color:#02c8a7; margin-bottom:14px; }
.items-list { max-height:400px; overflow-y:auto; padding-right:10px; }
.item { background:rgba(255,255,255,.05); border-left:3px solid #00a896; padding:10px 14px; margin-bottom:8px; border-radius:6px; display:flex; justify-content:space-between; align-items:center; gap:10px; }
.item .v { flex:1; font-size:14px; }
.item .u { font-size:11px; color:#888; }
.item button { background:#3a1a1a; color:#fff; border:none; border-radius:6px; padding:5px 10px; cursor:pointer; font-size:11px; }
.item button:hover { background:#ef4444; }
.actions { margin-top:20px; display:flex; gap:10px; flex-wrap:wrap; }
.actions a, .actions button { background:#0f3460; color:#fff; padding:10px 18px; border-radius:8px; text-decoration:none; border:none; cursor:pointer; font-weight:700; }
.actions a:hover, .actions button:hover { background:#00a896; }
.options-editor { margin-top:14px; background:rgba(0,0,0,.3); padding:14px; border-radius:8px; }
.options-editor textarea { width:100%; min-height:120px; background:rgba(0,0,0,.4); color:#fff; border:1px solid rgba(255,255,255,.15); border-radius:6px; padding:10px; font-family:inherit; font-size:13px; }
.options-editor button { background:#00a896; color:#fff; border:none; padding:8px 16px; border-radius:6px; margin-top:8px; cursor:pointer; }
.participants { background:#16213e; border-radius:14px; padding:18px; margin-bottom:24px; }
.participants h2 { color:#02c8a7; font-size:15px; margin-bottom:10px; }
.participants .chips { display:flex; flex-wrap:wrap; gap:8px; }
.participants .chip { background:rgba(0,168,150,.15); border:1px solid rgba(0,168,150,.3); border-radius:999px; padding:5px 12px; font-size:12px; }
</style></head>
<body>
<div class="container">
  <h1>Admin — Mestringstorget Workshop</h1>
  <div class="meta">Aktiv runde styrer hva deltakerne ser. Klikk pa en runde for a aktivere den.</div>

  <div class="participants">
    <h2 id="part-title">Deltakere (0)</h2>
    <div class="chips" id="part-chips"></div>
  </div>

  <div class="rounds" id="rounds"></div>

  <div class="live" id="live">
    <h2>Velg en aktiv runde for a se input live</h2>
  </div>

  <div class="actions" style="margin-top:30px">
    <a href="/wall" target="_blank">Apne Live Wall (storskjerm)</a>
    <a href="/p" target="_blank">Test deltaker-side</a>
    <a href="/api/export" download>Last ned full eksport (JSON)</a>
  </div>
</div>

<script>
let state = null;
let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onclose = () => setTimeout(connect, 1500);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.state) state = msg.state;
    else if (msg.type === 'item_added' && state) state.rounds[msg.round_id].items.push(msg.item);
    else if (msg.type === 'item_removed' && state) state.rounds[msg.round_id].items = state.rounds[msg.round_id].items.filter(i => i.id !== msg.item_id);
    else if (msg.type === 'vote_updated' && state) state.rounds[msg.round_id].votes = msg.votes;
    else if (msg.type === 'rating_updated' && state) state.rounds[msg.round_id].ratings = msg.ratings;
    else if (msg.type === 'journey_updated' && state) {
      const r = state.rounds[msg.round_id];
      if (r) { r.journeys = r.journeys || {}; r.journeys[msg.user_id] = msg.journey; }
    }
    else if (msg.type === 'journey_removed' && state) {
      const r = state.rounds[msg.round_id];
      if (r && r.journeys) delete r.journeys[msg.user_id];
    }
    else if (msg.type === 'participants_updated') { if (state) state.participants = msg.participants; }
    render();
  };
}

async function activate(rid) {
  await fetch(`/api/round/${rid}/activate`, {method:'POST'});
}

async function clearRound(rid) {
  if (!confirm('Slett alle lapper i denne runden?')) return;
  await fetch(`/api/round/${rid}/clear`, {method:'POST'});
}

function deleteItem(rid, itemId) {
  ws.send(JSON.stringify({type:'delete_item', round_id:rid, item_id:itemId}));
}

function setOptions(rid) {
  const ta = document.getElementById('opts-ta');
  const lines = ta.value.split('\\n').map(s => s.trim()).filter(Boolean);
  const opts = lines.map((text, i) => ({id: 'o'+i, text}));
  ws.send(JSON.stringify({type:'set_options', round_id:rid, options:opts}));
}

function render() {
  if (!state) return;
  // Participants
  const parts = Object.entries(state.participants);
  document.getElementById('part-title').textContent = `Deltakere (${parts.length})`;
  document.getElementById('part-chips').innerHTML = parts.map(([id, name]) => `<div class="chip">${escape(name || 'Anonym')}</div>`).join('');

  // Rounds grid
  const rounds = document.getElementById('rounds');
  rounds.innerHTML = Object.entries(state.rounds).map(([rid, r]) => {
    let count = 0;
    if (r.type === 'freetext' || r.type === 'categorized') count = (r.items || []).length;
    else if (r.type === 'vote') count = Object.values(r.votes || {}).reduce((s, arr) => s + arr.length, 0);
    else if (r.type === 'rating') count = (r.ratings || []).length;
    else if (r.type === 'journey') count = Object.keys(r.journeys || {}).length;
    return `
      <div class="round ${r.active ? 'active' : ''}">
        <h3>${escape(r.title)}</h3>
        <div class="desc">${escape((r.question || '').slice(0, 80))}</div>
        <div class="count">${count}</div>
        <button class="${r.active ? 'active' : ''}" onclick="activate('${rid}')">${r.active ? '✓ AKTIV' : 'Aktiver'}</button>
        <button class="danger" onclick="clearRound('${rid}')">Tom</button>
      </div>`;
  }).join('');

  // Live view of active round
  const live = document.getElementById('live');
  if (!state.active_round) {
    live.innerHTML = '<h2>Velg en aktiv runde</h2>';
    return;
  }
  const r = state.rounds[state.active_round];
  let html = `<h2>${escape(r.title)} — Live</h2>`;
  if (r.type === 'freetext' || r.type === 'categorized') {
    const items = r.items || [];
    html += `<div class="items-list">${items.map(i => `
      <div class="item">
        <div class="v">${i.category ? `<strong style="color:#02c8a7">[${i.category}]</strong> ` : ''}${escape(i.value)}</div>
        <div class="u">${escape(i.user_name || '')}</div>
        <button onclick="deleteItem('${state.active_round}','${i.id}')">slett</button>
      </div>`).join('') || '<div style="color:#888;text-align:center;padding:30px">Ingen lapper enna</div>'}</div>`;
  } else if (r.type === 'vote') {
    html += `<div class="options-editor">
      <div style="margin-bottom:10px;font-size:13px;color:#888">Skriv en valgmulighet per linje:</div>
      <textarea id="opts-ta">${(r.options || []).map(o => o.text).join('\\n')}</textarea>
      <button onclick="setOptions('${state.active_round}')">Oppdater valg</button>
    </div>`;
    const counts = {};
    (r.options || []).forEach(o => counts[o.id] = 0);
    Object.values(r.votes || {}).forEach(arr => arr.forEach(oid => { if (counts[oid] !== undefined) counts[oid]++; }));
    html += '<div style="margin-top:14px">' + (r.options || []).map(o => `<div class="item"><div class="v">${escape(o.text)}</div><div class="u" style="color:#02c8a7;font-weight:800;font-size:14px">${counts[o.id]}</div></div>`).join('') + '</div>';
  } else if (r.type === 'rating') {
    const ratings = r.ratings || [];
    const avg = ratings.length ? (ratings.reduce((s, x) => s + x.value, 0) / ratings.length).toFixed(2) : '-';
    html += `<div style="font-size:32px;color:#02c8a7;font-weight:800">Snitt: ${avg}</div>`;
    html += '<div class="items-list" style="margin-top:14px">' + ratings.map(rt => `<div class="item"><div class="v"><strong>${rt.value}</strong> ${escape(rt.comment || '')}</div><div class="u">${escape(rt.user_name)}</div></div>`).join('') + '</div>';
  } else if (r.type === 'journey') {
    const journeys = Object.entries(r.journeys || {});
    if (!journeys.length) {
      html += '<div style="color:#888;text-align:center;padding:30px">Ingen reiser sendt enna</div>';
    } else {
      html += '<div style="display:flex;flex-direction:column;gap:14px;margin-top:10px;">';
      journeys.forEach(([uid, j]) => {
        const stepsHtml = j.steps.map((s, i) => `<div style="background:rgba(0,168,150,.1);border:1px solid rgba(0,168,150,.3);padding:8px 12px;border-radius:8px;font-size:13px;">${i+1}. ${escape(s)}</div>`).join('<div style="color:#666;font-size:18px;align-self:center;">→</div>');
        html += `<div style="background:rgba(255,255,255,.04);padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,.08);">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong style="color:#02c8a7">${escape(j.user_name || 'Anonym')}</strong>
            <button onclick="deleteJourney('${state.active_round}','${uid}')" style="background:#3a1a1a;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;">slett</button>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${stepsHtml}</div>
        </div>`;
      });
      html += '</div>';
    }
  }
  live.innerHTML = html;
}

function deleteJourney(rid, uid) {
  if (!confirm('Slett denne reisen?')) return;
  ws.send(JSON.stringify({type:'delete_journey', round_id:rid, user_id:uid}));
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

connect();
</script>
</body></html>"""


# ---------- MAIN ----------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    ip = get_local_ip()
    print()
    print("=" * 60)
    print("MESTRINGSTORGET WORKSHOP SERVER")
    print("=" * 60)
    print(f"  Lokal IP:    {ip}")
    print(f"  Port:        {port}")
    print()
    print(f"  Fasilitator: http://{ip}:{port}/admin")
    print(f"  Live Wall:   http://{ip}:{port}/wall")
    print(f"  Deltakere:   http://{ip}:{port}/p")
    print()
    print(f"  Data lagres: {DATA_FILE}")
    print("=" * 60)
    print()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
