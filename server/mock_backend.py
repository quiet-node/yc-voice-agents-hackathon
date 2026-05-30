#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mock backend data for the Bayview Pharmacy secure refill demo.

This is the file to edit when customizing the demo: swap the patient records,
or replace the dict entirely with calls to a real backend (database, REST API,
etc.) from inside the tool functions in ``bot-nemotron.py``.

Patients are keyed by ``(name, date_of_birth)``. The lookup in the bot lowercases
and strips the name before matching, and expects the date of birth in ISO format
(``YYYY-MM-DD``) — the LLM normalizes whatever the caller says (e.g. "April 12th,
1985") into that shape before calling ``verify_identity``.

Each patient carries:
    id (str), prescriptions (list). Each prescription has:
        drug (str), refills_remaining (int), ready (bool — is it filled and
        waiting for pickup), last_filled (ISO date str).
"""

PATIENTS = {
    ("jane doe", "1985-04-12"): {
        "id": "p1",
        "prescriptions": [
            {
                "drug": "Lisinopril 10mg",
                "refills_remaining": 2,
                "ready": True,
                "last_filled": "2026-05-02",
            },
            {
                "drug": "Atorvastatin 20mg",
                "refills_remaining": 0,
                "ready": False,
                "last_filled": "2026-04-18",
            },
        ],
    },
    ("john smith", "1972-09-30"): {
        "id": "p2",
        "prescriptions": [
            {
                "drug": "Metformin 500mg",
                "refills_remaining": 5,
                "ready": False,
                "last_filled": "2026-05-10",
            },
        ],
    },
    ("maria garcia", "1990-11-23"): {
        "id": "p3",
        "prescriptions": [
            {
                "drug": "Levothyroxine 50mcg",
                "refills_remaining": 1,
                "ready": True,
                "last_filled": "2026-05-15",
            },
            {
                "drug": "Albuterol inhaler",
                "refills_remaining": 3,
                "ready": False,
                "last_filled": "2026-03-28",
            },
        ],
    },
    ("david lee", "1968-02-07"): {
        "id": "p4",
        "prescriptions": [
            {
                "drug": "Amlodipine 5mg",
                "refills_remaining": 0,
                "ready": False,
                "last_filled": "2026-04-30",
            },
        ],
    },
    ("susan brown", "1995-07-19"): {
        "id": "p5",
        "prescriptions": [
            {
                "drug": "Sertraline 50mg",
                "refills_remaining": 4,
                "ready": True,
                "last_filled": "2026-05-20",
            },
        ],
    },
}
