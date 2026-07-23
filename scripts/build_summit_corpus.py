#!/usr/bin/env python3
"""Build Summit Connect RAG corpus from JSON data files."""

import json
import sys
from pathlib import Path

import httpx

RAG_SERVICE_URL = "http://localhost:8004"
DATA_DIR = Path(__file__).parent.parent / "data" / "summit_connect"


def load_json(filename: str) -> dict:
    with open(DATA_DIR / filename) as f:
        return json.load(f)


def build_schedule_docs(schedule: dict) -> list[dict]:
    docs = []
    conf = schedule["conference"]
    docs.append({
        "text": f"{conf} runs {schedule['dates']} at {schedule['location']}.",
        "metadata": {"category": "schedule", "priority": "high"},
        "doc_id": "schedule_overview",
    })
    for day in schedule["days"]:
        for block in day["blocks"]:
            text = f"{day['label']} — {block['time']}: {block['title']}. Location: {block['location']}."
            if "speaker" in block:
                text += f" Presented by {block['speaker']}."
            docs.append({
                "text": text,
                "metadata": {"category": "schedule", "day": day["date"]},
                "doc_id": f"sched_{day['date']}_{block['time'].replace(':', '').replace('-', '_')}",
            })
    return docs


def build_session_docs(sessions: dict) -> list[dict]:
    docs = []
    for s in sessions["sessions"]:
        text = (
            f"Session: {s['title']}. Track: {s['track']}. Day {s['day']}, {s['time']}, Room {s['room']}. "
            f"Type: {s['type']}. {s['description']}"
        )
        docs.append({
            "text": text,
            "metadata": {"category": "sessions", "track": s["track"], "type": s["type"]},
            "doc_id": s["id"],
        })
    return docs


def build_speaker_docs(speakers: dict) -> list[dict]:
    docs = []
    for sp in speakers["speakers"]:
        text = (
            f"Speaker: {sp['name']}, {sp['title']} at {sp['company']}. {sp['bio']} "
            f"Speaking at {len(sp['sessions'])} session(s)."
        )
        docs.append({
            "text": text,
            "metadata": {"category": "speakers", "company": sp["company"]},
            "doc_id": sp["id"],
        })
    return docs


def build_venue_docs(venues: dict) -> list[dict]:
    docs = []
    mv = venues["main_venue"]
    docs.append({
        "text": f"Main venue: {mv['name']}, {mv['address']}. Amenities: {'; '.join(mv['amenities'])}.",
        "metadata": {"category": "venue"},
        "doc_id": "venue_main",
    })
    for level in mv["levels"]:
        areas = ", ".join(level["areas"])
        docs.append({
            "text": f"Level {level['level']}: {areas}.",
            "metadata": {"category": "venue"},
            "doc_id": f"venue_level_{level['level']}",
        })
    for hotel in venues["hotels"]:
        docs.append({
            "text": f"Hotel: {hotel['name']}, {hotel['distance']} from venue. {'Shuttle available.' if hotel['shuttle'] else ''} {hotel['rate']}.",
            "metadata": {"category": "hotels"},
            "doc_id": f"hotel_{hotel['name'].lower().replace(' ', '_')}",
        })
    for rest in venues["restaurants"]:
        docs.append({
            "text": f"Restaurant: {rest['name']} ({rest['cuisine']}), {rest['distance']} from venue. Price: {rest['price']}.",
            "metadata": {"category": "food"},
            "doc_id": f"food_{rest['name'].lower().replace(' ', '_')}",
        })
    t = venues["transport"]
    docs.append({
        "text": f"Transport: {t['shuttle']} {t['rideshare']} {t['parking']} {t['public_transit']}",
        "metadata": {"category": "transport"},
        "doc_id": "transport_info",
    })
    return docs


def build_city_docs(city: dict) -> list[dict]:
    docs = []
    docs.append({
        "text": f"Weather in {city['city']}: July average high {city['weather']['july_average_high']}, low {city['weather']['july_average_low']}. {city['weather']['conditions']}",
        "metadata": {"category": "weather"},
        "doc_id": "city_weather",
    })
    e = city["emergency"]
    docs.append({
        "text": f"Emergency: Event security — {e['event_security']}. First aid at {e['first_aid']}. Local emergency: {e['local_emergency']}. Nearest hospital: {e['nearest_hospital']}. Pharmacy: {e['pharmacy']}.",
        "metadata": {"category": "emergency", "priority": "high"},
        "doc_id": "city_emergency",
    })
    for attr in city["attractions"]:
        docs.append({
            "text": f"{attr['name']} ({attr['type']}), {attr['distance']} from venue: {attr['description']}",
            "metadata": {"category": "attractions"},
            "doc_id": f"attraction_{attr['name'].lower().replace(' ', '_')}",
        })
    for i, tip in enumerate(city["tips"]):
        docs.append({
            "text": tip,
            "metadata": {"category": "tips"},
            "doc_id": f"tip_{i}",
        })
    return docs


def build_architecture_docs(arch: dict) -> list[dict]:
    docs = []
    for entry in arch.get("architecture", []):
        docs.append({
            "text": entry["text"],
            "metadata": {"category": entry.get("category", "architecture")},
            "doc_id": entry["id"],
        })
    return docs


def main():
    print("Building Summit Connect RAG corpus...")

    all_docs = []
    all_docs.extend(build_schedule_docs(load_json("schedule.json")))
    all_docs.extend(build_session_docs(load_json("sessions.json")))
    all_docs.extend(build_speaker_docs(load_json("speakers.json")))
    all_docs.extend(build_venue_docs(load_json("venues.json")))
    all_docs.extend(build_city_docs(load_json("city_guide.json")))
    all_docs.extend(build_architecture_docs(load_json("architecture.json")))

    print(f"Generated {len(all_docs)} documents")

    loaded = 0
    failed = 0
    with httpx.Client(timeout=30.0) as client:
        for doc in all_docs:
            try:
                resp = client.post(
                    f"{RAG_SERVICE_URL}/add",
                    json={"doc_id": doc["doc_id"], "text": doc["text"], "metadata": doc["metadata"]},
                )
                if resp.status_code == 200:
                    loaded += 1
                else:
                    print(f"  WARN: {doc['doc_id']} — {resp.status_code}")
                    failed += 1
            except Exception as e:
                print(f"  ERROR: {doc['doc_id']} — {e}")
                failed += 1

    print(f"Done. Loaded: {loaded}, Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
