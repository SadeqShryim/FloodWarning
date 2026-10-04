"""Storm mode: injects a realistic new report every few seconds so the ranking reshuffles live.

Each injection mimics a real phone report: it arrives "pending" (gray, processing...), then 1.2-2.8 s
later its AI fields land and the urgency rules rank it. The pool locations are real Dearborn
intersections from OpenStreetMap (Overpass API: shared node of both streets, or the closest approach
for divided roads), different from the seed reports, and jittered by up to ~120 m per injection.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from typing import Any

from .seed_data import CALL_911, EXTRACTION_KEYS, report

log = logging.getLogger(__name__)

AddReport = Callable[[dict], Awaitable[dict]]  # insert a row dict, publish it, return the stored row (with id)
ApplyUpdate = Callable[[int, dict], Awaitable[dict]]  # update fields, recompute urgency, publish, return the row
OnState = Callable[[bool, int], Awaitable[None]]  # (running, injected) after every change

JITTER_M = 120.0

# 26 pre-written reports; 6 of them rank CRITICAL (tests/test_seed_storm.py checks it).
POOL: list[dict[str, Any]] = [
    report(
        place="Warren Ave & Reuter St", lat=42.34413, lng=-83.17200,  # Overpass: shared node
        lang="ar",
        said="الحقونا! جدّي قاعد على كرسي متحرك بالبيسمنت والمي عم تطلع بسرعة، صارت لنص إجريه. ما فينا نطلّعه لحالنا. وورن وروتر.",
        english="Help us! My grandfather is in a wheelchair in the basement and the water is coming up fast, it's halfway up his legs. We can't get him out by ourselves. Warren and Reuter.",
        summary="Grandfather in wheelchair stuck in basement, water rising fast",
        confirm="فهمنا إن جدّك على كرسي متحرك عالق بالبيسمنت والمي عم تعلى. بلّغنا فرق الإنقاذ. " + CALL_911["ar"],
        hint="Warren Ave and Reuter St", depth=30, where="basement", living=True, rising=True,
        people=("elderly", "disabled", "trapped"), count=3, needs=("evacuation",),
    ),
    report(
        place="Hubbard Dr & Greenfield Rd", lat=42.32212, lng=-83.19568,  # Overpass: shared node
        lang="en",
        said="Water's coming in the basement window wells, maybe four inches so far. No one's down there. Hubbard and Greenfield.",
        english="Water's coming in the basement window wells, maybe four inches so far. No one's down there. Hubbard and Greenfield.",
        summary="Water entering through basement window wells, about 10 cm",
        confirm="We understand water is coming in through the basement window wells, about 10 cm, at Hubbard and Greenfield. " + CALL_911["en"],
        hint="Hubbard Dr and Greenfield Rd", depth=10, where="basement",
    ),
    report(
        place="Southfield Fwy service drive & Rotunda Dr", lat=42.29845, lng=-83.20610,  # Overpass: shared node
        lang="en",
        said="I'm stuck in my car on the Southfield service drive by Rotunda. Water is over the hood and coming inside. My door won't open. My kid is in the back seat.",
        english="I'm stuck in my car on the Southfield service drive by Rotunda. Water is over the hood and coming inside. My door won't open. My kid is in the back seat.",
        summary="Parent and child trapped in car, water over the hood",
        confirm="We understand you and your child are trapped in your car on the Southfield service drive by Rotunda, water over the hood. " + CALL_911["en"],
        hint="Southfield service drive by Rotunda Dr", depth=70, where="car", rising=True,
        people=("children", "trapped"), count=2, needs=("evacuation",),
    ),
    report(
        place="Ford Rd & Greenfield Rd", lat=42.32914, lng=-83.19613,  # Overpass: closest approach
        lang="es",
        said="La calle Ford en Greenfield ya tiene agua hasta la banqueta, como veinte centímetros. Los carros están pasando despacio.",
        english="Ford Road at Greenfield already has water up to the curb, about twenty centimeters. Cars are going through slowly.",
        summary="Ford Rd at Greenfield flooded to the curb, about 20 cm",
        confirm="Entendimos que Ford y Greenfield tienen unos veinte centímetros de agua en la calle. " + CALL_911["es"],
        hint="Ford and Greenfield", depth=20, where="street",
    ),
    report(
        place="Dix Ave & Miller Rd", lat=42.29864, lng=-83.14927,  # Overpass: shared node
        lang="es",
        said="Mi esposo hace diálisis en casa y la máquina se apagó porque se fue la luz. El sótano está inundado. Necesitamos ayuda, estamos cerca de Dix y Miller.",
        english="My husband does dialysis at home and the machine shut off because the power went out. The basement is flooded. We need help, we're near Dix and Miller.",
        summary="Home dialysis machine down after power loss, basement flooded",
        confirm="Entendimos que la máquina de diálisis de su esposo se apagó por falta de luz y el sótano está inundado, cerca de Dix y Miller. " + CALL_911["es"],
        hint="Dix and Miller", depth=None, where="home",
        people=("medical",), count=2, needs=("medical", "evacuation"),
    ),
    report(
        place="Ferney St & Wyoming St", lat=42.30685, lng=-83.15084,  # Overpass: shared node
        lang="ar",
        said="السلام عليكم، المجاري راجعة في البدروم حقنا والريحة ما تنطاق، الماء حوالي ثلاثين سانتي. إحنا في فيرني جنب وايومنق.",
        english="Peace be upon you, the sewer is backing up into our basement and the smell is unbearable, the water is about thirty centimeters. We're on Ferney near Wyoming.",
        summary="Sewage backing up into basement, about 30 cm",
        confirm="وعليكم السلام. فهمنا إن المجاري راجعة في البدروم والماء حوالي ثلاثين سانتي، في فيرني قرب وايومنق. " + CALL_911["ar"],
        hint="Ferney St near Wyoming St", depth=30, where="basement",
        hazards=("sewage",), needs=("pumping",),
    ),
    report(
        place="Michigan Ave & Brady St", lat=42.30718, lng=-83.24004,  # Overpass: shared node
        lang="en",
        said="Michigan Avenue at Brady, the right lane is under water, about six inches. Just reporting it.",
        english="Michigan Avenue at Brady, the right lane is under water, about six inches. Just reporting it.",
        summary="Right lane of Michigan Ave at Brady under 15 cm of water",
        confirm="Thanks, we understand the right lane of Michigan Avenue at Brady has about 15 cm of water. " + CALL_911["en"],
        hint="Michigan Ave at Brady St", depth=15, where="street",
    ),
    report(
        place="Salina St & Vernor Hwy", lat=42.30376, lng=-83.14597,  # Overpass: shared node
        lang="ar",
        said="أبوي وقع على الدرج وهو ينزل للبدروم، راسه ينزف وما يقدر يقوم. الماء في البدروم للركبة. إحنا جنب سالينا وفيرنور، بسرعة الله يخليكم.",
        english="My father fell on the stairs going down to the basement, his head is bleeding and he can't get up. The water in the basement is knee-deep. We're near Salina and Vernor, please hurry.",
        summary="Father fell on basement stairs, head bleeding, cannot get up, knee-deep water",
        confirm="فهمنا إن والدك وقع على الدرج وراسه ينزف وما يقدر يقوم، والماء للركبة. بلّغنا فرق الإنقاذ. " + CALL_911["ar"],
        hint="Salina St and Vernor Hwy", depth=50, where="basement",
        people=("medical",), count=3, needs=("medical",),
    ),
    report(
        place="Tireman Ave & Neckel Ave", lat=42.35126, lng=-83.17838,  # Overpass: shared node
        lang="ar",
        said="شلونكم، السرداب بيه ماي شوية، يمكن عشر سانتي، والمضخة عطلانة. تايرمن ويا نيكل.",
        english="Hi, there's a little water in the basement, maybe ten centimeters, and the pump is broken. Tireman and Neckel.",
        summary="About 10 cm of water in basement, pump broken",
        confirm="فهمنا إن في السرداب حوالي عشر سانتي ماء والمضخة عاطلة، عند تايرمن ونيكل. " + CALL_911["ar"],
        hint="Tireman and Neckel", depth=10, where="basement", needs=("pumping",),
    ),
    report(
        place="Garrison St & Military St", lat=42.30569, lng=-83.25161,  # Overpass: shared node
        lang="en",
        said="My mom is eighty-five, she lives alone on Garrison near Military. She says water is coming into her basement and it's getting higher. She sounds scared.",
        english="My mom is eighty-five, she lives alone on Garrison near Military. She says water is coming into her basement and it's getting higher. She sounds scared.",
        summary="85-year-old living alone, basement water rising, sounds scared",
        confirm="We understand your 85-year-old mother is alone on Garrison near Military and water is rising in her basement. " + CALL_911["en"],
        hint="Garrison St near Military St", depth=20, where="basement", rising=True,
        people=("elderly",), count=1, needs=("evacuation",),
    ),
    report(
        place="Monroe St & Outer Dr", lat=42.29241, lng=-83.25015,  # Overpass: shared node
        lang="en",
        said="Big tree came down across Monroe at Outer Drive and the street is flooded behind it, maybe ten inches.",
        english="Big tree came down across Monroe at Outer Drive and the street is flooded behind it, maybe ten inches.",
        summary="Tree down across Monroe at Outer Dr, street flooded about 25 cm",
        confirm="We understand a tree is down across Monroe at Outer Drive and the street is flooded about 25 cm. " + CALL_911["en"],
        hint="Monroe St at Outer Dr", depth=25, where="street",
    ),
    report(
        place="Warren Ave & Kendal Ave", lat=42.34381, lng=-83.18534,  # Overpass: shared node
        lang="ar",
        said="المي دخلت ع غرفة القعدة بالطابق الأرضي، حوالي عشرين سانتي، هلّق وقفت. بس عنا ولاد صغار. وورن وكندال.",
        english="The water got into the living room on the ground floor, about twenty centimeters, it stopped now. But we have small kids. Warren and Kendal.",
        summary="20 cm of water in ground-floor living room, small children home",
        confirm="فهمنا إن المي فاتت ع غرفة القعدة حوالي عشرين سانتي وعندكن ولاد صغار. " + CALL_911["ar"],
        hint="Warren and Kendal", depth=20, where="home", living=True,
        people=("children",), count=5, needs=("evacuation",),
    ),
    report(
        place="Carlysle St & Outer Dr", lat=42.28438, lng=-83.23148,  # Overpass: shared node
        lang="es",
        said="Tenemos agua en el sótano, como veinte centímetros. Ya movimos las cosas. Carlysle y Outer Drive.",
        english="We have water in the basement, about twenty centimeters. We already moved our things. Carlysle and Outer Drive.",
        summary="About 20 cm of water in basement, belongings moved",
        confirm="Entendimos que tienen unos veinte centímetros de agua en el sótano, en Carlysle y Outer Drive. " + CALL_911["es"],
        hint="Carlysle and Outer Drive", depth=20, where="basement",
    ),
    report(
        place="Ford Rd & Horger St", lat=42.32950, lng=-83.17987,  # Overpass: shared node
        lang="ar",
        said="في مي بالبيسمنت ووصلت للبريزات، والضو عم يطفي ويشعل. نحنا ع فورد قريب من هورجر.",
        english="There's water in the basement and it reached the outlets, and the lights are flickering. We're on Ford near Horger.",
        summary="Basement water reached outlets, lights flickering",
        confirm="فهمنا إن المي وصلت للبريزات بالبيسمنت والضو عم يطفي ويشعل. ابعدوا عن البيسمنت والكهربا. " + CALL_911["ar"],
        hint="Ford Rd near Horger St", depth=25, where="basement",
        hazards=("electrical",), needs=("evacuation",),
    ),
    report(
        place="Hubbard Dr & Evergreen Rd", lat=42.32136, lng=-83.23135,  # Overpass: shared node
        lang="en",
        said="Hubbard and Evergreen, there's a car stalled in the intersection with water at the doors. Driver got out, he's okay.",
        english="Hubbard and Evergreen, there's a car stalled in the intersection with water at the doors. Driver got out, he's okay.",
        summary="Car stalled in intersection, water at the doors, driver out",
        confirm="Thanks, we understand a car is stalled at Hubbard and Evergreen with water at the doors and the driver is out. " + CALL_911["en"],
        hint="Hubbard Dr and Evergreen Rd", depth=40, where="car", count=1,
    ),
    report(
        place="Akron St & Lowrey St", lat=42.30720, lng=-83.14579,  # Overpass: shared node
        lang="es",
        said="Mi vecino es mayor y vive solo. Su sótano tiene como cuarenta centímetros de agua y huele a drenaje. Akron y Lowrey.",
        english="My neighbor is elderly and lives alone. His basement has about forty centimeters of water and it smells like sewage. Akron and Lowrey.",
        summary="Elderly neighbor alone, 40 cm of sewage water in basement",
        confirm="Entendimos que su vecino mayor vive solo y su sótano tiene unos cuarenta centímetros de agua con drenaje, en Akron y Lowrey. " + CALL_911["es"],
        hint="Akron and Lowrey", depth=40, where="basement",
        hazards=("sewage",), people=("elderly",), count=1, needs=("pumping",),
    ),
    report(
        place="Michigan Ave & Miller Rd", lat=42.32498, lng=-83.16626,  # Overpass: shared node
        lang="ar",
        said="شارع ميشيغن عند ميلر صار بحيرة، الماي يمكن خمسة وعشرين سانتي. السيارات واكفة.",
        english="Michigan Avenue at Miller has turned into a lake, the water is maybe twenty-five centimeters. The cars are stopped.",
        summary="Michigan Ave at Miller flooded about 25 cm, traffic stopped",
        confirm="فهمنا إن شارع ميشيغن عند ميلر فيه حوالي خمسة وعشرين سانتي ماء والسيارات واقفة. " + CALL_911["ar"],
        hint="Michigan Ave at Miller Rd", depth=25, where="street",
    ),
    report(
        place="Pelham St & Rotunda Dr", lat=42.29275, lng=-83.22901,  # Overpass: shared node
        lang="en",
        said="The basement wall bowed in and there's water coming through the crack. Maybe eight inches. Pelham near Rotunda.",
        english="The basement wall bowed in and there's water coming through the crack. Maybe eight inches. Pelham near Rotunda.",
        summary="Basement wall bowing in, water through crack, about 20 cm",
        confirm="We understand your basement wall is bowing in with water coming through, about 20 cm, on Pelham near Rotunda. " + CALL_911["en"],
        hint="Pelham St near Rotunda Dr", depth=20, where="basement",
        hazards=("structural",), needs=("pumping",),
    ),
    report(
        place="Warren Ave & Lonyo St", lat=42.34452, lng=-83.15588,  # Overpass: shared node
        lang="ar",
        said="عمي عنده ضيق نفس ويستعمل جهاز أكسجين، والكهرباء انقطعت من الفيضان. البدروم مليان ماء. إحنا في وورن عند لونيو.",
        english="My uncle has shortness of breath and uses an oxygen machine, and the electricity cut out from the flood. The basement is full of water. We're on Warren at Lonyo.",
        summary="Uncle on oxygen machine lost power, basement full of water",
        confirm="فهمنا إن عمك يستخدم جهاز أكسجين والكهرباء مقطوعة والبدروم مليان ماء. بلّغنا فرق الإنقاذ. " + CALL_911["ar"],
        hint="Warren Ave at Lonyo St", depth=60, where="basement",
        people=("elderly", "medical"), count=4, needs=("medical", "evacuation"),
    ),
    report(
        place="Nowlin St & Pelham St", lat=42.28774, lng=-83.23189,  # Overpass: shared node
        lang="en",
        said="Just a heads up, the storm drain on Nowlin at Pelham is backing up onto the street. Nothing inside the houses yet.",
        english="Just a heads up, the storm drain on Nowlin at Pelham is backing up onto the street. Nothing inside the houses yet.",
        summary="Storm drain backing up onto Nowlin at Pelham, no homes affected",
        confirm="Thanks, we understand the storm drain at Nowlin and Pelham is backing up onto the street. " + CALL_911["en"],
        hint="Nowlin St at Pelham St", depth=10, where="street",
    ),
    report(
        place="Hemlock Ave & Reuter St", lat=42.33503, lng=-83.17159,  # Overpass: shared node
        lang="es",
        said="Se nos metió el agua al sótano donde tenemos la sala, ya va por la rodilla. Estamos arriba, todos bien. Hemlock y Reuter.",
        english="Water got into the basement where we have the living room, it's already knee-high. We're upstairs, everyone's fine. Hemlock and Reuter.",
        summary="Finished basement flooded knee-high, family safe upstairs",
        confirm="Entendimos que el agua en la sala del sótano ya llega a la rodilla y que todos están arriba y bien. " + CALL_911["es"],
        hint="Hemlock and Reuter", depth=50, where="basement", living=True, needs=("pumping",),
    ),
    report(
        place="Military St & Rotunda Dr", lat=42.29215, lng=-83.23162,  # Overpass: shared node
        lang="ar",
        said="الماء في البدروم ريحته غاز، فتحنا الشبابيك وطلعنا. إحنا جنب ملتري وروتوندا.",
        english="The water in the basement smells like gas, we opened the windows and got out. We're near Military and Rotunda.",
        summary="Gas smell in flooded basement, family evacuated",
        confirm="فهمنا إن في ريحة غاز في البدروم وإنكم طلعتوا. لا تدخلوا البيت. " + CALL_911["ar"],
        hint="Military St and Rotunda Dr", depth=15, where="basement",
        hazards=("gas",), needs=("evacuation",),
    ),
    report(
        place="Michigan Ave & Telegraph Rd", lat=42.30004, lng=-83.27156,  # Overpass: closest approach
        lang="en",
        said="Michigan and Telegraph, two lanes flooded on the eastbound side, about a foot. Traffic is backing up.",
        english="Michigan and Telegraph, two lanes flooded on the eastbound side, about a foot. Traffic is backing up.",
        summary="Two eastbound lanes flooded about 30 cm at Michigan and Telegraph",
        confirm="Thanks, we understand two eastbound lanes at Michigan and Telegraph have about 30 cm of water. " + CALL_911["en"],
        hint="Michigan Ave and Telegraph Rd", depth=30, where="street",
    ),
    report(
        place="Steadman St & Warren Ave", lat=42.34375, lng=-83.18992,  # Overpass: shared node
        lang="ar",
        said="السرداب بيه ماي للركبة، وبنتي معاقة وتنام بالسرداب. هسة نقلناها فوق والماي وكف. ستيدمن ويا وورن.",
        english="The basement has knee-deep water, and my daughter is disabled and sleeps in the basement. We moved her upstairs now and the water has stopped. Steadman and Warren.",
        summary="Disabled daughter's basement bedroom flooded knee-deep, she is upstairs",
        confirm="فهمنا إن السرداب فيه ماء للركبة وبنتك نقلتوها فوق. " + CALL_911["ar"],
        hint="Steadman St and Warren Ave", depth=50, where="basement", living=True,
        people=("disabled",), count=3, needs=("pumping",),
    ),
    report(
        place="Cherry Hill St & Telegraph Rd", lat=42.31258, lng=-83.27196,  # Overpass: shared node
        lang="es",
        said="Solo para avisar: hay mucha agua en Cherry Hill y Telegraph, como quince centímetros, pero se puede pasar.",
        english="Just to let you know: there's a lot of water at Cherry Hill and Telegraph, about fifteen centimeters, but you can get through.",
        summary="About 15 cm of water at Cherry Hill and Telegraph, passable",
        confirm="Gracias, entendimos que hay unos quince centímetros de agua en Cherry Hill y Telegraph. " + CALL_911["es"],
        hint="Cherry Hill and Telegraph", depth=15, where="street",
    ),
    report(
        place="Ford Rd & Evergreen Rd", lat=42.32882, lng=-83.23503,  # Overpass: closest approach
        lang="en",
        said="Our basement is flooding near Ford and Evergreen, about a foot of water and it's still rising. Nobody hurt.",
        english="Our basement is flooding near Ford and Evergreen, about a foot of water and it's still rising. Nobody hurt.",
        summary="Basement flooding near Ford and Evergreen, about 30 cm and rising",
        confirm="We understand your basement has about 30 cm of water and it is still rising, near Ford and Evergreen. " + CALL_911["en"],
        hint="Ford Rd and Evergreen Rd", depth=30, where="basement", rising=True, needs=("pumping",),
    ),
]


def _jitter(lat: float, lng: float, rng: random.Random, max_m: float = JITTER_M) -> tuple[float, float]:
    """Move a point by up to max_m meters in a random direction (uniform over the disc)."""
    distance = max_m * math.sqrt(rng.random())
    bearing = rng.uniform(0, 2 * math.pi)
    dlat = distance * math.cos(bearing) / 111_320.0
    dlng = distance * math.sin(bearing) / (111_320.0 * math.cos(math.radians(lat)))
    return round(lat + dlat, 6), round(lng + dlng, 6)


def _empty_flags(item: dict[str, Any]) -> dict[str, Any]:
    """All-false copies of the nested flag dicts, for the row as it looks before the AI ran."""
    return {
        "hazards": {k: False for k in item["hazards"]},
        "people_at_risk": {**{k: False for k in item["people_at_risk"] if k != "count"}, "count": None},
        "needs": {k: False for k in item["needs"]},
    }


class StormController:
    """Drives storm mode. One injection loop task plus one short "finish" task per injected report.

    Stopping is cooperative: a stop event wakes the loop's sleep at once, and pending finishes
    apply their AI fields immediately instead of waiting out their delay, so no report is left
    stuck at "processing..." and no task outlives stop().
    """

    # The fake "AI latency" before a report's fields land. Tests shrink it on the instance.
    finish_delay_s: tuple[float, float] = (1.2, 2.8)

    def __init__(self, add_report: AddReport, apply_update: ApplyUpdate, on_state: OnState) -> None:
        self._add_report = add_report
        self._apply_update = apply_update
        self._on_state = on_state
        self._running = False
        self._injected = 0
        self._next_item = 0  # keeps cycling through POOL across runs, so a second storm brings new places
        self._task: asyncio.Task | None = None
        self._finishers: set[asyncio.Task] = set()
        self._stop_event: asyncio.Event | None = None
        self._rng = random.Random()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def injected(self) -> int:
        """Reports injected by the current (or last) storm run."""
        return self._injected

    def start(self, *, max_reports: int = 24, min_interval_s: float = 2.0, max_interval_s: float = 4.5) -> None:
        """Start injecting (no-op if already running). Must be called from inside the event loop."""
        if self._running:
            return
        loop = asyncio.get_running_loop()
        min_interval_s = max(0.0, float(min_interval_s))
        max_interval_s = max(min_interval_s, float(max_interval_s))
        self._running = True
        self._injected = 0
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(
            self._run(max(0, int(max_reports)), min_interval_s, max_interval_s, self._stop_event),
            name="storm-loop",
        )

    async def stop(self) -> None:
        """Stop injecting (no-op if not running). Flushes pending finishes, then reports the new state."""
        was_running = self._running
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            try:
                # The loop wakes on the stop event; give an in-flight add_report a moment to finish.
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                log.exception("storm loop ended with an error")
        await self._flush_finishers()
        if was_running:
            await self._notify()

    # ------------------------------------------------------------ internals

    async def _run(self, max_reports: int, min_s: float, max_s: float, stop_event: asyncio.Event) -> None:
        await self._notify()
        try:
            while not stop_event.is_set() and self._injected < max_reports:
                try:
                    await self._inject_one(stop_event)
                except Exception:
                    log.exception("storm injection failed; continuing")
                if stop_event.is_set():
                    break
                await self._notify()
                if self._injected >= max_reports:
                    break
                try:
                    # Sleep for the interval, but wake immediately when stop() is called.
                    await asyncio.wait_for(stop_event.wait(), timeout=self._rng.uniform(min_s, max_s))
                except asyncio.TimeoutError:
                    pass
        finally:
            ended_naturally = not stop_event.is_set()
            if ended_naturally:
                self._running = False
        if ended_naturally:
            await self._notify()  # stop() sends its own notification

    async def _inject_one(self, stop_event: asyncio.Event) -> None:
        item = POOL[self._next_item % len(POOL)]
        self._next_item += 1
        lat, lng = _jitter(item["lat"], item["lng"], self._rng)
        row = {
            "lat": lat,
            "lng": lng,
            "accuracy_m": float(self._rng.choice((8, 12, 16, 25, 40))),
            "location_source": "storm",
            "address_text": item["address_text"],
            "ui_language": item["ui_language"],
            "input_type": item["input_type"],
            "water_in_living_space": False,
            "water_rising": False,
            **_empty_flags(item),
            "status": "new",
            "ai_status": "pending",
            "is_simulated": True,
        }
        stored = await self._add_report(row)
        self._injected += 1
        report_id = stored.get("id") if stored else None
        if report_id is None:
            log.warning("storm: add_report returned no id; skipping its finish")
            return
        delay = self._rng.uniform(*self.finish_delay_s)
        finisher = asyncio.get_running_loop().create_task(
            self._finish(int(report_id), item, delay, stop_event), name=f"storm-finish-{report_id}"
        )
        self._finishers.add(finisher)
        finisher.add_done_callback(self._finishers.discard)

    async def _finish(self, report_id: int, item: dict[str, Any], delay: float, stop_event: asyncio.Event) -> None:
        """After a fake AI delay, land the extraction fields (immediately if the storm is stopped)."""
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        fields = {key: item[key] for key in EXTRACTION_KEYS}
        fields["hazards"] = dict(item["hazards"])
        fields["people_at_risk"] = dict(item["people_at_risk"])
        fields["needs"] = dict(item["needs"])
        fields.update(
            ai_status="done",
            ai_engine="storm-sim",
            ai_error=None,
            ai_latency_ms=int(delay * 1000) + self._rng.randint(0, 250),
        )
        try:
            await self._apply_update(report_id, fields)
        except Exception:
            log.exception("storm: finishing report %s failed", report_id)

    async def _flush_finishers(self) -> None:
        pending = list(self._finishers)
        if not pending:
            return
        done, still_pending = await asyncio.wait(pending, timeout=5.0)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    async def _notify(self) -> None:
        try:
            await self._on_state(self._running, self._injected)
        except Exception:
            log.exception("storm: on_state callback failed")
