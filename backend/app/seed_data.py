"""The 25 demo reports spread across Dearborn, as if the last two and a half hours of a storm had happened.

Every location is a real intersection (never a house number). Coordinates come from OpenStreetMap
via the Overpass API: the node shared by both named streets, or for divided roads the midpoint of
the closest approach of the two streets. All were checked to fall inside Dearborn's city boundary
and away from the Rouge River.

Mix (checked by tests/test_seed_storm.py against app.urgency): 4 CRITICAL, 8 HIGH, 7 MEDIUM, 6 LOW;
10 English, 9 Arabic (Lebanese, Yemeni and Iraqi flavored), 6 Spanish; 4 dispatched, 2 resolved.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .util import to_iso

HAZARD_KEYS = ("electrical", "sewage", "gas", "structural")
PEOPLE_KEYS = ("elderly", "children", "disabled", "medical", "trapped")
NEED_KEYS = ("evacuation", "pumping", "medical", "supplies")

# The fields the AI step fills in. storm.py sends exactly these when a simulated report "finishes".
EXTRACTION_KEYS = (
    "language",
    "transcript_original",
    "transcript_english",
    "ai_summary",
    "confirmation_message",
    "water_depth_cm",
    "location_type",
    "location_hint",
    "water_in_living_space",
    "water_rising",
    "hazards",
    "people_at_risk",
    "needs",
)


def report(
    *,
    place: str,
    lat: float,
    lng: float,
    lang: str,
    said: str,
    english: str,
    summary: str,
    confirm: str,
    hint: str | None,
    depth: int | None,
    where: str,
    living: bool = False,
    rising: bool = False,
    hazards: tuple[str, ...] = (),
    people: tuple[str, ...] = (),
    count: int | None = None,
    needs: tuple[str, ...] = (),
    input_type: str = "voice",
) -> dict[str, Any]:
    """One pre-written report (shared by the seeds and the storm pool), flags given as tuples of names."""
    return {
        "address_text": place,
        "lat": lat,
        "lng": lng,
        "ui_language": lang if lang in ("en", "ar", "es") else "en",
        "input_type": input_type,
        "language": lang,
        "transcript_original": said,
        "transcript_english": english,
        "ai_summary": summary,
        "confirmation_message": confirm,
        "location_hint": hint,
        "water_depth_cm": depth,
        "location_type": where,
        "water_in_living_space": living,
        "water_rising": rising,
        "hazards": {k: k in hazards for k in HAZARD_KEYS},
        "people_at_risk": {**{k: k in people for k in PEOPLE_KEYS}, "count": count},
        "needs": {k: k in needs for k in NEED_KEYS},
    }


# 911 reminders, in the reporter's language (the brief: every confirmation ends with it).
CALL_911 = {
    "en": "If anyone's life is in danger, call 911.",
    "ar": "إذا كانت حياة أحد في خطر، اتصلوا بـ 911.",
    "es": "Si hay una vida en peligro, llame al 911.",
}

# (number, minutes ago, status, ai_latency_ms, seed_audio or None, report)
_SEEDS: list[tuple[int, int, str, int, str | None, dict[str, Any]]] = [
    # ---------------------------------------------------------------- CRITICAL
    (1, 4, "new", 3120, "seed_01_ar.mp3", report(
        place="Paul Ave & Schaefer Rd", lat=42.33674, lng=-83.17653,  # Overpass: shared node
        lang="ar",
        said="ألو؟ دخيلكن ساعدونا! إمي كبيرة بالعمر وهي بالبيسمنت وما فيها تطلع الدرج. المي عم تعلى بسرعة، صارت فوق ركبتها. نحنا بشارع بول، قريب من شيفر.",
        english="Hello? Please, help us! My mother is elderly, she's in the basement and can't climb the stairs. The water is rising fast, it's above her knee. We're on Paul Avenue, near Schaefer.",
        summary="Elderly mother trapped in basement, water above knee and rising fast",
        confirm="فهمنا إن والدتك الكبيرة بالعمر عالقة بالبيسمنت والمي فوق ركبتها وعم تعلى. بلّغنا فرق الإنقاذ. " + CALL_911["ar"],
        hint="Paul Ave near Schaefer Rd", depth=60, where="basement", living=True, rising=True,
        people=("elderly", "trapped"), count=2, needs=("evacuation",),
    )),
    (2, 11, "dispatched", 2480, "seed_02_en.mp3", report(
        place="Southfield Fwy service drive & Michigan Ave", lat=42.31106, lng=-83.21456,  # Overpass: closest approach
        lang="en",
        said="My car stalled on the Southfield service drive right at Michigan Avenue. The water is up to the windows and I can't get the door open. Please send someone, it keeps coming up.",
        english="My car stalled on the Southfield service drive right at Michigan Avenue. The water is up to the windows and I can't get the door open. Please send someone, it keeps coming up.",
        summary="Driver trapped in stalled car, water up to the windows and rising",
        confirm="We understand you are trapped in your car on the Southfield service drive at Michigan Avenue, with water up to the windows. Rescue teams have your report. " + CALL_911["en"],
        hint="Southfield service drive at Michigan Ave", depth=85, where="car", rising=True,
        people=("trapped",), count=1, needs=("evacuation",),
    )),
    (3, 23, "new", 3870, "seed_03_es.mp3", report(
        place="Dix Ave & Vernor Hwy", lat=42.30418, lng=-83.14509,  # Overpass: shared node
        lang="es",
        said="Hola, mi papá usa oxígeno en casa y se fue la luz porque se inundó el sótano. El concentrador no funciona y le cuesta respirar. Estamos en Dix y Vernor.",
        english="Hi, my dad uses oxygen at home and the power went out because the basement flooded. The concentrator isn't working and he's having trouble breathing. We're at Dix and Vernor.",
        summary="Man on home oxygen lost power after basement flood, trouble breathing",
        confirm="Entendimos que su papá usa oxígeno, se fue la luz y le cuesta respirar, en Dix y Vernor. Los equipos de rescate ya tienen su reporte. " + CALL_911["es"],
        hint="Dix and Vernor", depth=None, where="home",
        people=("medical",), count=2, needs=("medical", "evacuation"),
    )),
    (4, 37, "new", 2950, None, report(
        place="Hemlock Ave & Miller Rd", lat=42.33514, lng=-83.16668,  # Overpass: shared node
        lang="ar",
        said="يا جماعة الخير، البدروم حقنا مليان ماء، والماء وصل لصندوق الكهرباء وشفنا شرار، وعندنا جهال صغار في البيت. إحنا جنب شارع ميلر وهيملوك. ايش نسوي؟",
        english="Folks, our basement is full of water, the water reached the electrical panel and we saw sparks, and we have small kids in the house. We're near Miller and Hemlock. What should we do?",
        summary="Basement flooded up to electrical panel, sparks seen, young children home",
        confirm="فهمنا إن الماء وصل لصندوق الكهرباء في البدروم وفي شرار، وعندكم أطفال صغار. ابتعدوا عن البدروم والكهرباء. " + CALL_911["ar"],
        hint="Miller Rd and Hemlock Ave", depth=60, where="basement",
        hazards=("electrical",), people=("children",), needs=("evacuation",),
    )),
    # ---------------------------------------------------------------- HIGH
    (5, 9, "new", 2210, None, report(
        place="Gould Ave & Neckel Ave", lat=42.34579, lng=-83.17814,  # Overpass: shared node
        lang="en",
        said="Our finished basement has about a foot and a half of water and sewage is coming up through the floor drain. It smells terrible. We're on Neckel near Gould.",
        english="Our finished basement has about a foot and a half of water and sewage is coming up through the floor drain. It smells terrible. We're on Neckel near Gould.",
        summary="Finished basement with 46 cm of water, sewage backing up through drain",
        confirm="We understand your finished basement has about 46 cm of water with sewage coming up the drain, on Neckel near Gould. " + CALL_911["en"],
        hint="Neckel Ave near Gould Ave", depth=46, where="basement", living=True,
        hazards=("sewage",), needs=("pumping",),
    )),
    (6, 16, "new", 3340, "seed_06_ar.mp3", report(
        place="Colson St & Horger St", lat=42.32469, lng=-83.17960,  # Overpass: shared node
        lang="ar",
        said="شلونكم، المجاري طافحة بالسرداب مالتنا، الريحة كلش قوية، والماي واصل تقريباً عشرين سانتي. إحنا بشارع كولسن يم هورجر.",
        english="Hi, the sewer is backing up into our basement, the smell is really strong, and the water is about twenty centimeters. We're on Colson Street by Horger.",
        summary="Sewage backing up into basement, about 20 cm of water",
        confirm="فهمنا إن المجاري طافحة في السرداب والماء حوالي عشرين سانتي، في شارع كولسن قرب هورجر. " + CALL_911["ar"],
        hint="Colson St by Horger St", depth=20, where="basement",
        hazards=("sewage",), needs=("pumping",),
    )),
    (7, 52, "dispatched", 2760, None, report(
        place="Colson St & Schaefer Rd", lat=42.32518, lng=-83.17608,  # Overpass: shared node
        lang="es",
        said="Mi abuela de ochenta años vive conmigo. Tenemos como treinta centímetros de agua en el sótano. No está subiendo y ella está arriba, bien, pero nos preocupa. Estamos en Colson y Schaefer.",
        english="My eighty-year-old grandmother lives with me. We have about thirty centimeters of water in the basement. It's not rising and she's upstairs and okay, but we're worried. We're at Colson and Schaefer.",
        summary="80-year-old grandmother at home, 30 cm water in basement, not rising",
        confirm="Entendimos que hay unos treinta centímetros de agua en el sótano y que su abuela de ochenta años está arriba y bien. " + CALL_911["es"],
        hint="Colson and Schaefer", depth=30, where="basement",
        people=("elderly",), count=2, needs=("pumping",),
    )),
    (8, 28, "new", 1980, None, report(
        place="Michigan Ave & Greenfield Rd", lat=42.31628, lng=-83.19485,  # Overpass: closest approach
        lang="en",
        said="There's a crack in our basement wall and water is pouring in through it. Maybe a foot of water now and it's still coming. We're just off Michigan and Greenfield.",
        english="There's a crack in our basement wall and water is pouring in through it. Maybe a foot of water now and it's still coming. We're just off Michigan and Greenfield.",
        summary="Cracked basement wall pouring water, about 30 cm and rising",
        confirm="We understand water is pouring through a crack in your basement wall, about 30 cm and still rising, near Michigan and Greenfield. " + CALL_911["en"],
        hint="Michigan Ave and Greenfield Rd", depth=30, where="basement", rising=True,
        hazards=("structural",), needs=("pumping",),
    )),
    (9, 44, "new", 3560, None, report(
        place="Warren Ave & Chase Rd", lat=42.34379, lng=-83.18657,  # Overpass: shared node
        lang="ar",
        said="مرحبا، في ريحة غاز قوية بالبيسمنت، المي وصلت لعند فرن التدفئة. طلعنا كلنا لبرّا. نحنا ع وورن قريب من تشايس.",
        english="Hello, there's a strong gas smell in the basement, the water reached the furnace. We all went outside. We're on Warren near Chase.",
        summary="Strong gas smell after water reached furnace, family outside",
        confirm="فهمنا إن في ريحة غاز قوية بالبيسمنت بعد ما وصلت المي للفرن، وإنكم كلكم برّا. ضلّوا بعاد عن البيت. " + CALL_911["ar"],
        hint="Warren Ave near Chase Rd", depth=None, where="basement",
        hazards=("gas",), needs=("evacuation",),
    )),
    (10, 131, "resolved", 2630, None, report(
        place="Military St & Morley Ave", lat=42.30678, lng=-83.25200,  # Overpass: shared node
        lang="en",
        said="We have two toddlers and their bedroom is in the finished basement. There's about fourteen inches of water down there. We moved them upstairs but we need help pumping.",
        english="We have two toddlers and their bedroom is in the finished basement. There's about fourteen inches of water down there. We moved them upstairs but we need help pumping.",
        summary="Toddlers' basement bedroom flooded, 36 cm water, kids moved upstairs",
        confirm="We understand there are about 36 cm of water in your finished basement and the two toddlers are upstairs now. " + CALL_911["en"],
        hint="Military St and Morley Ave", depth=36, where="basement", living=True,
        people=("children",), count=4, needs=("pumping",),
    )),
    (11, 67, "new", 4010, None, report(
        place="Dix Ave & Salina St", lat=42.30292, lng=-83.14608,  # Overpass: shared node
        lang="ar",
        said="السلام عليكم، الماء في البدروم أسود ومعه مجاري، طالع من البلاعة، حوالي شبر. إحنا في الساوث إند جنب دكس وسالينا.",
        english="Peace be upon you, the water in the basement is black with sewage, coming up from the drain, about a hand span deep. We're in the Southend near Dix and Salina.",
        summary="Black sewage water rising from basement drain, about 20 cm",
        confirm="وعليكم السلام. فهمنا إن في ماء مجاري في البدروم حوالي شبر، في الساوث إند قرب دكس وسالينا. " + CALL_911["ar"],
        hint="Southend, Dix and Salina", depth=20, where="basement",
        hazards=("sewage",), needs=("pumping",),
    )),
    (12, 82, "new", 2890, None, report(
        place="Rotunda Dr & Schaefer Rd", lat=42.31059, lng=-83.17546,  # Overpass: shared node
        lang="es",
        said="Se metió el agua al cuarto del sótano donde duerme mi hermano. Hay como medio metro de agua. Estamos bien, pero perdimos casi todo. Rotunda y Schaefer.",
        english="Water got into the basement room where my brother sleeps. There's about half a meter of water. We're okay, but we lost almost everything. Rotunda and Schaefer.",
        summary="Basement bedroom flooded with half a meter of water, family safe",
        confirm="Entendimos que hay medio metro de agua en el cuarto del sótano y que ustedes están bien, en Rotunda y Schaefer. " + CALL_911["es"],
        hint="Rotunda and Schaefer", depth=50, where="basement", living=True,
        needs=("pumping", "supplies"),
    )),
    # ---------------------------------------------------------------- MEDIUM
    (13, 19, "new", 2340, "seed_13_en.mp3", report(
        place="Hemlock Ave & Orchard Ave", lat=42.33476, lng=-83.18735,  # Overpass: shared node
        lang="en",
        said="Hi, my sump pump quit and there's maybe six inches of water in the basement. It's unfinished and nobody's down there. Hemlock and Orchard. We just need a pump if anyone has one.",
        english="Hi, my sump pump quit and there's maybe six inches of water in the basement. It's unfinished and nobody's down there. Hemlock and Orchard. We just need a pump if anyone has one.",
        summary="Sump pump failed, 15 cm in unfinished basement, needs a pump",
        confirm="We understand your sump pump failed and there are about 15 cm of water in the unfinished basement, at Hemlock and Orchard. " + CALL_911["en"],
        hint="Hemlock and Orchard", depth=15, where="basement", needs=("pumping",),
    )),
    (14, 58, "dispatched", 3210, None, report(
        place="Calhoun St & Paul Ave", lat=42.33676, lng=-83.17530,  # Overpass: shared node
        lang="ar",
        said="السيارة طفت بنص الشارع، الماي واصل للأبواب. آني طلعت منها وواقف على الرصيف، بس السيارة سادّة الطريق. شارع كالهون ويا بول.",
        english="The car died in the middle of the street, the water is up to the doors. I got out and I'm standing on the sidewalk, but the car is blocking the road. Calhoun Street and Paul.",
        summary="Car stalled in 40 cm of water blocking road, driver out and safe",
        confirm="فهمنا إن السيارة واقفة بالماء عند كالهون وبول وإنك طلعت منها وأنت بأمان. " + CALL_911["ar"],
        hint="Calhoun St and Paul Ave", depth=40, where="car", count=1,
    )),
    (15, 74, "new", 2050, None, report(
        place="Lowrey St & Salina St", lat=42.30638, lng=-83.14786,  # Overpass: shared node
        lang="es",
        said="Buenas tardes, en el sótano tenemos unos diez centímetros de agua. Necesitamos una bomba para sacarla. Calle Lowrey y Salina.",
        english="Good afternoon, we have about ten centimeters of water in the basement. We need a pump to get it out. Lowrey and Salina.",
        summary="10 cm of water in basement, family needs a pump",
        confirm="Entendimos que tienen unos diez centímetros de agua en el sótano y necesitan una bomba, en Lowrey y Salina. " + CALL_911["es"],
        hint="Lowrey and Salina", depth=10, where="basement", needs=("pumping",),
    )),
    (16, 33, "new", 1870, None, report(
        place="Ford Rd & Chase Rd", lat=42.32944, lng=-83.18594,  # Overpass: shared node
        lang="en",
        said="My car died in the water at Ford and Chase. It's about a foot deep here. I'm out of the car and safe, but it's stuck in the right lane.",
        english="My car died in the water at Ford and Chase. It's about a foot deep here. I'm out of the car and safe, but it's stuck in the right lane.",
        summary="Car stalled in 30 cm of water at Ford and Chase, driver safe",
        confirm="We understand your car stalled in about 30 cm of water at Ford and Chase and you are out and safe. " + CALL_911["en"],
        hint="Ford Rd and Chase Rd", depth=30, where="car", count=1,
    )),
    (17, 6, "new", 2570, None, report(
        place="Kendal Ave & Paul Ave", lat=42.33660, lng=-83.18519,  # Overpass: shared node
        lang="ar",
        said="عم تفوت المي ع البيسمنت من الحيطان، لهلّق شوي بس عم تزيد. نحنا ع كندال وبول.",
        english="Water is coming into the basement through the walls, just a little so far but it's increasing. We're on Kendal and Paul.",
        summary="Water seeping through basement walls, shallow but rising",
        confirm="فهمنا إن المي عم تفوت ع البيسمنت من الحيطان وعم تزيد، عند كندال وبول. " + CALL_911["ar"],
        hint="Kendal and Paul", depth=5, where="basement", rising=True,
    )),
    (18, 96, "new", 2190, None, report(
        place="Garrison St & Mason St", lat=42.30683, lng=-83.24650,  # Overpass: shared node
        lang="en",
        said="Water came in under the back door into our family room, about three inches. We're on Garrison by Mason. Everyone's fine, just letting you know.",
        english="Water came in under the back door into our family room, about three inches. We're on Garrison by Mason. Everyone's fine, just letting you know.",
        summary="About 8 cm of water in ground-floor family room, everyone fine",
        confirm="We understand about 8 cm of water came into your family room on Garrison by Mason and everyone is fine. " + CALL_911["en"],
        hint="Garrison St by Mason St", depth=8, where="home", living=True,
    )),
    (19, 109, "new", 3480, None, report(
        place="Gould Ave & Wyoming St", lat=42.34631, lng=-83.15730,  # Overpass: shared node
        lang="ar",
        said="البدروم فيه ماء حوالي عشرين سانتي، ما في أحد تحت. إحنا في شارع قولد جنب وايومنق.",
        english="The basement has about twenty centimeters of water, nobody's down there. We're on Gould Street near Wyoming.",
        summary="About 20 cm of water in basement, nobody down there",
        confirm="فهمنا إن في البدروم حوالي عشرين سانتي ماء وما في أحد تحت، في شارع قولد قرب وايومنق. " + CALL_911["ar"],
        hint="Gould Ave near Wyoming St", depth=20, where="basement",
    )),
    # ---------------------------------------------------------------- LOW
    (20, 14, "new", 1910, None, report(
        place="Rotunda Dr & Greenfield Rd", lat=42.30554, lng=-83.18681,  # Overpass: closest approach
        lang="en",
        said="Rotunda at Greenfield is flooded curb to curb, maybe ten inches. Cars are turning around. Somebody should block it off.",
        english="Rotunda at Greenfield is flooded curb to curb, maybe ten inches. Cars are turning around. Somebody should block it off.",
        summary="Rotunda at Greenfield flooded curb to curb, about 25 cm",
        confirm="We understand Rotunda at Greenfield is flooded curb to curb, about 25 cm deep. " + CALL_911["en"],
        hint="Rotunda Dr at Greenfield Rd", depth=25, where="street",
    )),
    (21, 41, "dispatched", 2680, "seed_21_es.mp3", report(
        place="Michigan Ave & Monroe St", lat=42.30625, lng=-83.24404,  # Overpass: shared node
        lang="es",
        said="Las alcantarillas en Michigan y Monroe están tapadas y la calle está llena de agua, como quince centímetros. Nadie está en peligro, solo para que sepan.",
        english="The storm drains at Michigan and Monroe are clogged and the street is full of water, about fifteen centimeters. Nobody is in danger, just so you know.",
        summary="Clogged storm drains, 15 cm of water on Michigan at Monroe",
        confirm="Entendimos que las alcantarillas en Michigan y Monroe están tapadas y hay unos quince centímetros de agua en la calle. " + CALL_911["es"],
        hint="Michigan and Monroe", depth=15, where="street",
    )),
    (22, 148, "resolved", 2240, None, report(
        place="Warren Ave & Wyoming St", lat=42.34450, lng=-83.15724,  # Overpass: shared node
        lang="ar",
        said="بس حبيت خبّركن إنو في مي متجمعة ع وورن قريب من وايومنغ، حوالي عشرة سانتي، قدّام المحلات. ما في حدا بخطر.",
        english="Just wanted to let you know there's water pooling on Warren near Wyoming, about ten centimeters, in front of the shops. Nobody is in danger.",
        summary="Water pooling on Warren near Wyoming, about 10 cm, no one at risk",
        confirm="شكراً، فهمنا إن في مي متجمعة ع وورن قريب من وايومنغ حوالي عشرة سانتي. " + CALL_911["ar"],
        hint="Warren Ave near Wyoming St", depth=10, where="street",
    )),
    (23, 63, "new", 2050, None, report(
        place="Cherry Hill St & Outer Dr", lat=42.31153, lng=-83.26164,  # Overpass: shared node
        lang="en",
        said="Cherry Hill is under water by Outer Drive, about eight inches across both lanes. Drivers are going through it slowly.",
        english="Cherry Hill is under water by Outer Drive, about eight inches across both lanes. Drivers are going through it slowly.",
        summary="Cherry Hill by Outer Drive under 20 cm of water, still passable",
        confirm="We understand Cherry Hill by Outer Drive has about 20 cm of water across both lanes. " + CALL_911["en"],
        hint="Cherry Hill by Outer Dr", depth=20, where="street",
    )),
    (24, 121, "new", 3020, None, report(
        place="Oakwood Blvd & Rotunda Dr", lat=42.29489, lng=-83.21960,  # Overpass: shared node
        lang="es",
        said="La calle Oakwood cerca de Rotunda se está llenando de agua y sigue subiendo. Ya cerraron un carril. No hay nadie en peligro.",
        english="Oakwood near Rotunda is filling with water and it keeps rising. One lane is already closed. Nobody is in danger.",
        summary="Oakwood near Rotunda filling with water, one lane closed",
        confirm="Entendimos que Oakwood cerca de Rotunda se está llenando de agua y ya hay un carril cerrado. " + CALL_911["es"],
        hint="Oakwood near Rotunda", depth=None, where="street", rising=True,
    )),
    (25, 140, "new", 1830, None, report(
        place="Military St & Outer Dr", lat=42.31836, lng=-83.25603,  # Overpass: shared node
        lang="en",
        said="Power is out on our street near Outer Dr and Military. No water inside the house, just a big puddle in the yard. FYI.",
        english="Power is out on our street near Outer Dr and Military. No water inside the house, just a big puddle in the yard. FYI.",
        summary="Power out near Outer Dr and Military, no water inside",
        confirm="Thanks, we understand the power is out near Outer Dr and Military and there is no water inside. " + CALL_911["en"],
        hint="Outer Dr and Military St", depth=None, where="other", input_type="text",
    )),
]


def seed_rows(now: datetime) -> list[dict]:
    """Row dicts ready for db.insert_report (urgency is computed on insert). May carry a "seed_audio" filename."""
    rows = []
    for number, minutes_ago, status, latency_ms, audio, base in _SEEDS:
        created = to_iso(now - timedelta(minutes=minutes_ago))
        row = {
            **base,
            "hazards": dict(base["hazards"]),  # fresh copies: callers may mutate rows
            "people_at_risk": dict(base["people_at_risk"]),
            "needs": dict(base["needs"]),
            "created_at": created,
            "updated_at": created,
            "accuracy_m": None,
            "location_source": "seed",
            "status": status,
            "ai_status": "done",
            "ai_engine": "seed",
            "ai_error": None,
            "ai_latency_ms": latency_ms,
            "is_simulated": True,
            "audio_path": None,
            "audio_mime": None,
            "photo_path": None,
            "photo_mime": None,
        }
        if audio:
            row["seed_audio"] = audio
        rows.append(row)
    return rows


def seed_audio_texts() -> dict[str, tuple[str, str]]:
    """{seed_audio filename: (language, transcript_original)} for scripts/make_seed_audio.py."""
    return {audio: (base["language"], base["transcript_original"]) for _, _, _, _, audio, base in _SEEDS if audio}
