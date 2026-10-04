"""No-LLM fallback: rough keyword extraction from typed text (English, Arabic, Spanish).

Used when Gemini is not configured or fails on a typed report. It is deliberately simple:
a list of phrases per concept and language, a crude negation check ("nobody hurt",
"nadie herido", "ما في حدا مصاب") and a few depth patterns. It will miss things; the urgency
rules then keep failed reports at MEDIUM or above so nothing sinks unseen.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .models import Extraction, Hazards, Needs, PeopleAtRisk

LANGS = ("en", "ar", "es")

# Arabic letters (without digits/punctuation), used for "word boundary" checks in Arabic.
_AR_LETTER = "ء-يٱ-ۓ"
_ARABIC_SCRIPT = re.compile(r"[؀-ۿ]")
_AR_PREFIX = rf"(?<![{_AR_LETTER}])(?:وبال|وال|بال|عال|لل|ال|وب|ول|و|ف|ب|ل|ك|ع)?"
_AR_SUFFIX = rf"(?:تي|تها|ته|تو|ها|هم|هن|نا|كن|كم|ي|ك|ه|ة|ات|ين|ون)?(?![{_AR_LETTER}])"
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Latin-script patterns are wrapped in \b...\b and matched case-insensitively.
# Spanish patterns accept the word with or without accents, because people type fast.
# Arabic patterns are plain substrings (Arabic glues prefixes like ب، ل، و onto words).
_CONCEPTS: dict[str, dict[str, list[str]]] = {
    "trapped": {
        "en": [
            r"trapped", r"stuck (?:in|inside|down)", r"(?:can'?t|cannot|can not|unable to|could ?n'?t) (?:get|go|climb) (?:out|up)",
            r"(?:can'?t|cannot|can not|unable to) (?:leave|escape|climb the stairs|get up the stairs|walk up)",
            r"no way out",
        ],
        "ar": [
            "محاصر", "عالق", "محبوس", "محجوز",
            r"ما\s*(?:فينا|فيها|فيه|فيني|فيي|بقدر|بتقدر|بيقدر|منقدر|عم تقدر|عم يقدر|عم نقدر)\s+\S*طلع",
            "ما منقدر نطلع", "لا نستطيع الخروج", "لا تستطيع الخروج", "لا يستطيع الخروج",
        ],
        "es": [
            r"atrapad[oa]s?", r"no (?:podemos|puede|puedo|pueden) salir", r"no (?:puede|pueden|podemos) subir",
            r"encerrad[oa]s?", r"sin salida",
        ],
    },
    "elderly": {
        "en": [
            r"grand(?:ma|mother|pa|father|parents?)", r"elderly", r"old (?:man|woman|lady|people|couple)",
            r"senior(?: citizen)?s?", r"aged (?:mother|father)", r"my (?:\d{2}|eighty|ninety)[- ]year[- ]old",
        ],
        "ar": ["جدتي", "جدي", "ستي", "تيتا", "جدتنا", "جدنا", "كبير بالعمر", "كبيرة بالعمر", "كبار بالعمر", "كبير بالسن",
               "كبيرة بالسن", "مسن", "مسنة", "عجوز", "ختيار", "ختيارة", "الحجة", "الحجي"],
        "es": [r"abuel[oa]s?", r"ancian[oa]s?", r"persona(?:s)? mayor(?:es)?", r"señora mayor", r"senora mayor",
               r"señor mayor", r"senor mayor", r"tercera edad"],
    },
    "children": {
        "en": [r"kids?", r"child(?:ren)?", r"bab(?:y|ies)", r"toddlers?", r"infants?", r"newborn", r"little ones"],
        "ar": ["ولاد", "أولاد", "اولاد", "أطفال", "اطفال", "طفل", "طفلة", "بيبي", "رضيع", "زغار", "صغار", "ولادي"],
        "es": [r"niñ[oa]s?", r"nin[oa]s?", r"beb[eé]s?", r"criaturas?", r"chiquit[oa]s?", r"mis hij[oa]s? pequeñ[oa]s"],
    },
    "disabled": {
        "en": [r"wheel ?chair", r"disabled", r"disability", r"can'?t walk", r"cannot walk", r"paraly[sz]ed",
               r"bed-?ridden", r"bedbound", r"walker"],
        "ar": ["كرسي متحرك", "كرسي مدولب", "معاق", "معاقة", "مقعد على كرسي", "ما بيقدر يمشي", "ما بتقدر تمشي",
               "ما فيه يمشي", "ما فيها تمشي", "مشلول", "مشلولة", "ذوي الاحتياجات"],
        "es": [r"silla de ruedas", r"discapacitad[oa]s?", r"no (?:puede|pueden) caminar", r"paralizad[oa]",
               r"en cama", r"andadera"],
    },
    "medical": {
        "en": [
            r"oxygen", r"dialysis", r"injur(?:ed|y|ies)", r"hurt", r"bleeding", r"unconscious", r"passed out",
            r"not breathing", r"(?:can'?t|cannot|trouble) breath(?:e|ing)", r"chest pains?", r"heart attack",
            r"seizures?", r"broken (?:leg|arm|bone)", r"ventilator", r"insulin",
        ],
        "ar": ["أكسجين", "اكسجين", "أوكسجين", "اوكسجين", "غسيل كلى", "غسيل كلاوي", "غسيل الكلى", "مصاب", "مصابة",
               "مجروح", "مجروحة", "جريح", "نزيف", "فاقد الوعي", "فاقدة الوعي", "مغمى عليه", "مغمى عليها",
               "غايب عن الوعي", "غايبة عن الوعي", "وجع بالصدر", "وجع في الصدر", "ألم في الصدر", "الم بالصدر",
               "ما عم يتنفس", "ما عم تتنفس", "ضيق نفس", "ضيقة نفس", "جلطة", "نوبة قلبية"],
        "es": [r"ox[ií]geno", r"di[aá]lisis", r"herid[oa]s?", r"lastimad[oa]s?", r"sangrando", r"inconsciente",
               r"desmayad[oa]", r"dolor (?:de|en el) pecho", r"no (?:puede|puedo) respirar", r"infarto",
               r"convulsi[oó]n", r"lesionad[oa]s?"],
    },
    "electrical": {
        "en": [r"outlets?", r"(?:electrical|electric|breaker|fuse|power) (?:panel|box)", r"panel", r"electrical",
               r"electricity", r"electrocut\w*", r"sparks?", r"sparking", r"wires?", r"wiring", r"power lines?",
               r"breakers?", r"buzzing"],
        "ar": ["كهربا", "كهرباء", "الكهربة", "شرار", "شرارة", "أسلاك", "اسلاك", "شريط كهرب", "فيش", "بريز",
               "لوحة الكهرباء", "طبلون", "قاطع"],
        "es": [r"chispas?", r"chispea\w*", r"cables?", r"luz", r"enchufes?", r"tomacorrientes?",
               r"panel(?: el[eé]ctrico)?", r"el[eé]ctric[oa]s?", r"electricidad", r"caja de fusibles", r"breakers?"],
    },
    "sewage": {
        "en": [r"sewage", r"sewers?", r"septic", r"toilets? (?:is |are )?(?:backing|overflowing)", r"raw waste"],
        "ar": ["مجاري", "مجارير", "صرف صحي", "الصرف الصحي", "مي وسخة", "مياه الصرف"],
        "es": [r"aguas negras", r"drenajes?", r"alcantarillas?", r"aguas residuales", r"cloacas?"],
    },
    "gas": {
        "en": [r"(?:smell|smells|smelling) (?:of |like )?gas", r"gas (?:smell|leak|leaking|odou?r)", r"leaking gas"],
        "ar": ["ريحة غاز", "ريحة الغاز", "رائحة غاز", "رائحة الغاز", "تسرب غاز", "تسريب غاز", "الغاز عم يسرب"],
        "es": [r"olor a gas", r"huele a gas", r"fuga de gas", r"escape de gas"],
    },
    "structural": {
        "en": [r"collaps(?:e|ed|ing)", r"cracks?", r"cracked", r"cracking", r"cav(?:e|ed|ing) in", r"ceiling (?:fell|is falling|falling)",
               r"foundation", r"wall (?:fell|is falling|came down)", r"sinkhole"],
        "ar": ["انهيار", "انهار", "انهارت", "وقع السقف", "وقع الحيط", "السقف عم يوقع", "تشقق", "تشققات", "شق بالحيط",
               "شقوق", "فسخ بالحيط"],
        "es": [r"derrumb\w*", r"colaps\w*", r"grietas?", r"agrietad[oa]", r"se cay[oó] (?:el techo|la pared)",
               r"socav[oó]n", r"cimientos"],
    },
    "basement": {
        "en": [r"basements?", r"cellar", r"lower level", r"downstairs"],
        "ar": ["بيسمنت", "قبو", "سرداب", "بدروم", "البدروم", "الطابق السفلي"],
        "es": [r"s[oó]tanos?"],
    },
    # Someone is *in* a vehicle (just mentioning cars on a flooded street is street flooding).
    "in_car": {
        "en": [r"in (?:my|the|our|a|his|her|their) (?:car|truck|van|minivan|suv|vehicle)", r"(?:car|truck|van) (?:is )?stalled",
               r"stalled (?:car|truck|van)", r"stuck in (?:traffic|the underpass)"],
        "ar": ["بالسيارة", "بسيارتي", "بسيارته", "بسيارتها", "في السيارة", "جوا السيارة", "داخل السيارة",
               "السيارة طفت", "السيارة علقت", "علقنا بالسيارة"],
        "es": [r"(?:en|dentro de) (?:el|mi|la|nuestro|su) (?:carro|coche|auto|camioneta|veh[ií]culo)",
               r"(?:carro|coche|auto) (?:se )?(?:qued[oó]|apag[oó]) (?:atorad|parad|atascad)\w*"],
    },
    "car": {
        "en": [r"cars?", r"vehicles?", r"trucks?", r"tires?", r"wheels?"],
        "ar": ["سيارة", "سيارات", "السيارة", "سيارتي", "دواليب", "الدواليب"],
        "es": [r"carros?", r"coches?", r"autos?", r"camionetas?", r"llantas?", r"ruedas?", r"neum[aá]ticos?"],
    },
    "street": {
        "en": [r"streets?", r"roads?", r"avenue", r"ave", r"intersection", r"underpass", r"viaduct", r"freeway",
               r"highway", r"service drive", r"sidewalk", r"block"],
        "ar": ["شارع", "الشارع", "طريق", "الطريق", "نفق", "تقاطع", "الأوتوستراد", "الفريواي", "الرصيف"],
        "es": [r"calles?", r"avenida", r"carretera", r"esquina", r"cruce", r"paso a desnivel", r"autopista",
               r"banqueta", r"acera"],
    },
    "home": {
        "en": [r"house", r"home", r"living room", r"bedrooms?", r"kitchen", r"first floor", r"ground floor",
               r"apartment", r"inside"],
        "ar": ["البيت", "بيتنا", "بيتي", "الغرفة", "غرفة النوم", "الصالون", "المطبخ", "الشقة", "جوا البيت"],
        "es": [r"casa", r"sala", r"cuarto", r"rec[aá]mara", r"cocina", r"apartamento", r"primer piso", r"adentro"],
    },
    # Someone lives in or is inside the basement, so it counts as living space.
    "occupied": {
        "en": [r"finished basement", r"(?:lives?|sleeps?|living|sleeping|bedroom) (?:in|downstairs)", r"bedroom",
               r"(?:is|are) (?:in|down in) the basement"],
        "ar": ["ساكن", "ساكنة", "ساكنين", "نايم", "نايمة", "غرفة نوم", "غرفتها", "غرفته"],
        "es": [r"vive en el s[oó]tano", r"duerme", r"recámara", r"rec[aá]mara", r"est[aá] en el s[oó]tano"],
    },
    "rising": {
        "en": [r"rising", r"keeps? (?:going|coming) up", r"(?:going|coming) up", r"getting (?:higher|deeper|worse)",
               r"still coming in", r"pouring in"],
        "ar": [r"عم (?:ي|ت)طلع", "يرتفع", "ترتفع", "عم يرتفع", "عم ترتفع", r"عم (?:ي|ت)زيد", "عم تعلى", "عم يعلى",
               "عم تفوت", "عم يفوت"],
        "es": [r"sube", r"subiendo", r"est[aá] subiendo", r"aumenta\w*", r"crece", r"creciendo", r"sigue entrando"],
    },
}

# Depth by body landmark, in cm (the prompt uses the same scale).
_BODY_DEPTH: list[tuple[int, dict[str, list[str]]]] = [
    (10, {"en": [r"ankles?(?:[- ]deep)?"], "ar": ["للكاحل", "لكاحل", "للكعب"], "es": [r"tobillos?"]}),
    (30, {"en": [r"mid[- ]?shin", r"shins?", r"half(?:way)? up (?:the |our |my )?(?:car )?(?:tires|wheels)", r"tires", r"wheels"],
          "ar": ["لنص الدواليب", "نص الدولاب", "الدواليب", "للساق"],
          "es": [r"mitad de (?:las )?(?:llantas|ruedas)", r"llantas", r"ruedas", r"espinillas?"]}),
    (50, {"en": [r"knee[- ]?(?:deep|high)", r"knees?"], "ar": ["للركبة", "للركب", "لركب", "لحد الركب", "ع الركب", "الركبة"],
          "es": [r"rodillas?"]}),
    (75, {"en": [r"thighs?(?:[- ]deep|[- ]high)?"], "ar": ["للفخذ", "لفخاذ", "للفخاذ"], "es": [r"muslos?"]}),
    (100, {"en": [r"waist[- ]?(?:deep|high)?", r"hips?"], "ar": ["للخصر", "لخصر", "للوسط", "لوسط"], "es": [r"cintura"]}),
    (130, {"en": [r"chest[- ](?:deep|high)", r"up to (?:my|the|his|her|our) chests?"], "ar": ["للصدر", "لصدر"],
           "es": [r"hasta el pecho"]}),
]

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "a couple": 2, "couple": 2, "half a": 0.5, "half": 0.5,
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "medio": 0.5, "media": 0.5,
}
_NUM = r"(\d+(?:[.,]\d+)?|half an?|a couple(?: of)?|couple|an?|one|two|three|four|five|six|seven|eight|nine|ten|uno|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|medio|media)"
_UNIT_PATTERNS: list[tuple[str, float]] = [
    (_NUM + r"\s*(?:-|\s)?(?:feet|foot|ft\b|pies|pie\b|قدم|اقدام|أقدام)", 30.48),
    (_NUM + r"\s*(?:-|\s)?(?:inches|inch|pulgadas?|إنش|انش|انشات)", 2.54),
    (_NUM + r"\s*(?:-|\s)?(?:centimeters?|centimetres?|cent[ií]metros?|cm\b|سانتي\w*|سم\b|سنتيمتر\w*)", 1.0),
    (_NUM + r"\s*(?:-|\s)?(?:meters?|metres?|metros?|m\b|متر|أمتار)", 100.0),
]
_AR_FEET = [("قدمين", 61), ("نص متر", 50), ("نصف متر", 50), ("متر", 100)]

_NEGATORS = re.compile(
    r"\b(?:no|not|nobody|no one|none|nothing|without|isn'?t|aren'?t|nadie|ningun[oa]?|ningún|sin|ni)\b"
    r"|ما في|مافي|ما حدا|ولا حدا|بدون|مش |ما فيه حدا|لا يوجد|ما في حد|محدش",
    re.IGNORECASE,
)
# Concepts whose own phrases already contain a negation ("can't get out") are still checked:
# the window looks only at the words *before* the match.

_COUNT_PATTERNS = [
    re.compile(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|dos|tres|cuatro|cinco|seis|siete|ocho)\s+"
               r"(?:people|persons|of us|kids|children|adults|personas|niñ[oa]s|ninos|adultos)\b", re.IGNORECASE),
    re.compile(r"\b(?:we are|we're|there are|somos|hay)\s+(\d+|two|three|four|five|six|seven|eight|dos|tres|cuatro|cinco|seis)\b",
               re.IGNORECASE),
    re.compile(r"(?:نحنا|نحن|احنا|إحنا)\s+(\d+)"),
    re.compile(r"(\d+)\s+(?:ولاد|أولاد|اولاد|أطفال|اطفال|اشخاص|أشخاص|أنفار|انفار)"),
]

# Dearborn street names (English, plus how people write them in Arabic script), so a place
# said in any language becomes something Nominatim/Overpass can find.
_STREETS: list[tuple[str, list[str]]] = [
    ("Warren", ["وارن", "ورن"]), ("Schaefer", ["شيفر", "شايفر", "شافر", "شيفير"]), ("Miller", ["ميلر"]),
    ("Wyoming", ["وايومنغ", "وايومينغ", "وايومينج"]), ("Ford Rd", ["فورد رود", "شارع فورد"]),
    ("Michigan Ave", ["ميشيغان", "ميشغن", "ميتشيغان"]), ("Dix", ["ديكس"]), ("Vernor", ["فيرنور", "فرنور"]),
    ("Salina", ["سالينا"]), ("Chase", ["تشيس", "تشايس"]), ("Greenfield", ["غرينفيلد", "جرينفيلد"]),
    ("Oakwood", ["اوكوود", "أوكوود"]), ("Southfield", ["ساوثفيلد"]), ("Telegraph", ["تلغراف"]),
    ("Outer Dr", ["أوتر درايف", "اوتر درايف"]), ("Cherry Hill", ["تشيري هيل", "شيري هيل"]),
    ("Rotunda", ["روتوندا"]), ("Monroe", ["مونرو"]), ("Military", ["ميليتري"]), ("Brady", ["برادي"]),
    ("Hubbard", ["هابرد"]), ("Tireman", ["تايرمان"]), ("Evergreen", ["ايفرغرين", "إيفرغرين"]),
    ("Lonyo", ["لونيو"]), ("Calhoun", ["كالهون"]), ("Morley", ["مورلي"]), ("Gulley", ["غولي"]),
    ("Kendal", ["كيندال"]), ("Mercury", ["ميركوري"]), ("Hemlock", ["هيملوك"]), ("Fairlane", ["فيرلين"]),
    ("Springwells", ["سبرينغويلز"]), ("Wagner", ["واغنر"]), ("Snow", []), ("Neckel", ["نيكل"]),
    ("Middlepointe", []), ("Reuter", []), ("Orchard", []), ("Mason", []), ("Steadman", []),
]

_RECEIVED_VOICE = {
    "en": "We received your voice note. A responder will listen to it. If life is in danger, call 911.",
    "ar": "وصلتنا رسالتك الصوتية. سيستمع إليها أحد المستجيبين. إذا كانت هناك حياة في خطر، اتصل بـ 911.",
    "es": "Recibimos tu nota de voz. Un rescatista la va a escuchar. Si hay una vida en peligro, llama al 911.",
}

# Pieces of the localized confirmation: (en, ar, es).
_FACT_TEXT = {
    "trapped": ("someone cannot get out", "في شخص ما بيقدر يطلع", "alguien no puede salir"),
    "elderly": ("an elderly person", "شخص كبير بالعمر", "una persona mayor"),
    "children": ("children", "أطفال", "niños"),
    "disabled": ("a person with a disability", "شخص من ذوي الاحتياجات", "una persona con discapacidad"),
    "medical": ("a medical need", "حاجة طبية", "una necesidad médica"),
    "electrical": ("water near electricity", "مي قريبة من الكهربا", "agua cerca de la electricidad"),
    "sewage": ("sewage", "مجاري", "aguas negras"),
    "gas": ("a gas smell", "ريحة غاز", "olor a gas"),
    "structural": ("structural damage", "ضرر بالبناء", "daño en la estructura"),
    "rising": ("water rising", "المي عم تطلع", "el agua sube"),
    "basement": ("in the basement", "بالقبو", "en el sótano"),
    "car": ("in a car", "بالسيارة", "en un carro"),
    "street": ("street flooding", "فيضان بالشارع", "calle inundada"),
    "home": ("inside the home", "جوا البيت", "dentro de la casa"),
}
_CONFIRM = {
    "en": ("We received your report", "A responder will review it. If life is in danger, call 911."),
    "ar": ("وصلنا بلاغك", "سيراجعه أحد المستجيبين. إذا كانت هناك حياة في خطر، اتصل بـ 911."),
    "es": ("Recibimos tu reporte", "Un rescatista lo revisará. Si hay una vida en peligro, llama al 911."),
}
_WATER_DEPTH_TEXT = {"en": "about {} cm of water", "ar": "حوالي {} سم مي", "es": "unos {} cm de agua"}

_compiled: dict[tuple[str, str], list[re.Pattern[str]]] = {}


def _patterns(concept_table: dict[str, list[str]], lang: str, key: str) -> list[re.Pattern[str]]:
    cache_key = (key, lang)
    if cache_key not in _compiled:
        out = []
        for p in concept_table.get(lang, []):
            if lang == "ar":
                # Arabic glues clitics onto words (و، ب، ل، ال...) and pronoun endings after them.
                # Allow those, but nothing else, so "جدي" (grandpa) does not match "جديد" (new)
                # and "قاطع" (breaker) does not match "تقاطع" (intersection).
                out.append(re.compile(_AR_PREFIX + "(?:" + p + ")" + _AR_SUFFIX))
            else:
                out.append(re.compile(r"(?<![\w'])" + p + r"(?![\w'])", re.IGNORECASE))
        _compiled[cache_key] = out
    return _compiled[cache_key]


def _negated(text: str, start: int) -> bool:
    """True when the few words before a match (same clause) contain a negation."""
    window = text[max(0, start - 28):start]
    # Only look inside the current clause.
    window = re.split(r"[.!?;,،؛\n]", window)[-1]
    return bool(_NEGATORS.search(window))


def _has(text: str, concept: str, *, negatable: bool = True) -> bool:
    """Does any language's phrase list for this concept match? (People mix languages.)"""
    table = _CONCEPTS[concept]
    for lang in LANGS:
        for pattern in _patterns(table, lang, concept):
            for m in pattern.finditer(text):
                if not negatable or not _negated(text, m.start()):
                    return True
    return False


def detect_language(text: str, ui_language: str = "en") -> str:
    """Arabic script -> ar; Spanish markers -> es; else en. The UI language breaks ties."""
    if not text:
        return ui_language if ui_language in LANGS else "en"
    arabic_chars = len(_ARABIC_SCRIPT.findall(text))
    latin_chars = len(re.findall(r"[A-Za-zÀ-ÿ]", text))
    if arabic_chars and arabic_chars >= latin_chars * 0.3:
        return "ar"
    lowered = text.lower()
    es_score = len(re.findall(r"[ñ¿¡áéíóú]", lowered)) + len(re.findall(
        r"\b(?:el|la|los|las|de|del|que|y|en|es|est[aá]|hay|agua|mi|mis|nuestra|nuestro|por favor|ayuda|"
        r"calle|casa|no podemos|tengo|tenemos|hasta|cerca|muy|con|para|una|unos|nadie)\b", lowered))
    en_score = len(re.findall(
        r"\b(?:the|is|are|and|in|of|my|our|we|water|it|there|please|help|have|has|to|near|up|on|with|no one)\b",
        lowered))
    if es_score > en_score:
        return "es"
    if en_score > es_score:
        return "en"
    return ui_language if ui_language in ("en", "es") else "en"


def _to_number(token: str) -> float | None:
    token = token.strip().lower().replace(",", ".")
    token = re.sub(r"\s+of$", "", token)
    try:
        return float(token)
    except ValueError:
        return _NUMBER_WORDS.get(token)


def extract_depth_cm(text: str) -> int | None:
    """Deepest water mentioned, in cm: numbers with units first, then body landmarks."""
    text = text.translate(_ARABIC_DIGITS)
    found: list[float] = []
    for pattern, factor in _UNIT_PATTERNS:
        for m in re.finditer(r"(?<![\w])" + pattern, text, re.IGNORECASE):
            n = _to_number(m.group(1))
            if n is not None:
                found.append(n * factor)
    for word, cm in _AR_FEET:
        if word in text and not found:
            found.append(cm)
    for cm, table in _BODY_DEPTH:
        key = f"depth{cm}"
        for lang in LANGS:
            if any(p.search(text) for p in _patterns(table, lang, key)):
                found.append(cm)
    if not found:
        return None
    return max(0, min(500, round(max(found))))


def _people_count(text: str) -> int | None:
    text = text.translate(_ARABIC_DIGITS)
    for pattern in _COUNT_PATTERNS:
        m = pattern.search(text)
        if m:
            n = _to_number(m.group(1))
            if n is not None and 1 <= n <= 200:
                return int(n)
    return None


def extract_location_hint(text: str) -> str | None:
    """Known Dearborn street names mentioned in any script -> "Warren and Schaefer" style hint."""
    found: list[tuple[int, str]] = []
    for english, arabic in _STREETS:
        bare = english.split()[0]
        m = re.search(r"\b" + re.escape(bare) + r"\b", text, re.IGNORECASE)
        positions = [m.start()] if m else []
        positions += [text.find(a) for a in arabic if a in text]
        if positions:
            found.append((min(positions), english))
    if not found:
        return None
    names = [name for _, name in sorted(found)][:2]
    return " and ".join(names)


def _localized_facts(facts: Iterable[str], lang: str) -> list[str]:
    idx = LANGS.index(lang)
    return [_FACT_TEXT[f][idx] for f in facts if f in _FACT_TEXT]


def extract_from_text(text: str, ui_language: str = "en") -> Extraction:
    """Best-effort extraction without an LLM. Never raises."""
    try:
        return _extract(text or "", ui_language)
    except Exception:  # noqa: BLE001 - the fallback must never take a report down with it
        cleaned = (text or "").strip()
        lang = ui_language if ui_language in LANGS else "en"
        return Extraction(
            language=lang,
            transcript_original=cleaned,
            transcript_english=cleaned if lang == "en" else "[not translated - AI offline] " + cleaned,
            ai_summary="Typed report - needs human review",
            confirmation_message=_CONFIRM[lang][0] + ". " + _CONFIRM[lang][1],
        )


def _extract(text: str, ui_language: str) -> Extraction:
    cleaned = " ".join(text.split())
    lang = detect_language(cleaned, ui_language)

    flags = {c: _has(cleaned, c) for c in (
        "trapped", "elderly", "children", "disabled", "medical", "electrical", "sewage", "gas",
        "structural", "basement", "in_car", "street", "home", "occupied", "rising",
    )}
    flags["car_mention"] = _has(cleaned, "car", negatable=False)
    depth = extract_depth_cm(cleaned)

    # Where is the water? Someone in a car beats everything; then basement, home, street.
    if flags["in_car"]:
        location_type = "car"
    elif flags["basement"]:
        location_type = "basement"
    elif flags["home"]:
        location_type = "home"
    elif flags["street"] or flags["car_mention"]:
        location_type = "street"
    else:
        location_type = "other"

    # A basement counts as living space when someone vulnerable is down there or it is lived in.
    living = flags["occupied"] or (location_type == "home" and (depth or 0) > 0) or (
        location_type == "basement" and (flags["elderly"] or flags["disabled"] or flags["trapped"])
    )

    people = PeopleAtRisk(
        elderly=flags["elderly"],
        children=flags["children"],
        disabled=flags["disabled"],
        medical=flags["medical"],
        trapped=flags["trapped"],
        count=_people_count(cleaned),
    )
    hazards = Hazards(
        electrical=flags["electrical"], sewage=flags["sewage"], gas=flags["gas"], structural=flags["structural"]
    )
    needs = Needs(
        evacuation=flags["trapped"] or (flags["rising"] and (flags["elderly"] or flags["disabled"] or flags["children"])),
        pumping=location_type in ("basement", "home") and (depth or 0) > 0,
        medical=flags["medical"],
        supplies=False,
    )

    # Facts in priority order, reused for the English summary and the localized confirmation.
    facts = [f for f in ("trapped", "medical", "elderly", "children", "disabled", "electrical", "gas",
                         "sewage", "structural") if flags[f]]
    place = {"basement": "basement", "car": "car", "home": "home", "street": "street"}.get(location_type)

    summary = _summary_en(flags, location_type, depth, people)
    confirmation = _confirmation(lang, facts, place, depth, flags["rising"])

    return Extraction(
        language=lang,
        transcript_original=cleaned,
        transcript_english=cleaned if lang == "en" else "[not translated - AI offline] " + cleaned,
        ai_summary=summary,
        confirmation_message=confirmation,
        water_depth_cm=depth,
        location_type=location_type,
        location_hint=extract_location_hint(cleaned),
        water_in_living_space=bool(living),
        water_rising=flags["rising"],
        hazards=hazards,
        people_at_risk=people,
        needs=needs,
    )


def _summary_en(flags: dict, location_type: str, depth: int | None, people: PeopleAtRisk) -> str:
    """One short English line for responders, e.g. "Elderly person trapped in basement, 50 cm water rising"."""
    who = []
    if flags["elderly"]:
        who.append("elderly person")
    if flags["children"]:
        who.append("children")
    if flags["disabled"]:
        who.append("disabled person")
    subject = " and ".join(who) if who else ""
    if subject:
        subject = subject[0].upper() + subject[1:]
    if flags["trapped"]:
        subject = (subject + " trapped") if subject else "Person trapped"
    place = {"basement": "in basement", "car": "in car", "home": "at home", "street": "street flooding"}.get(location_type)

    parts = []
    head = " ".join(p for p in (subject, place) if p)
    if head:
        parts.append(head[0].upper() + head[1:])
    water = f"{depth} cm water" if depth else ("water" if flags["rising"] else "")
    if flags["rising"] and water:
        water += " rising"
    if water:
        parts.append(water)
    extras = []
    if flags["medical"]:
        extras.append("medical need")
    if flags["electrical"]:
        extras.append("electrical hazard")
    if flags["gas"]:
        extras.append("gas smell")
    if flags["sewage"]:
        extras.append("sewage")
    if flags["structural"]:
        extras.append("structural damage")
    if extras:
        parts.append(", ".join(extras))
    if not parts:
        return "Flood report - details unclear, needs review"
    line = ", ".join(parts)
    return line[0].upper() + line[1:]


def _confirmation(lang: str, facts: list[str], place: str | None, depth: int | None, rising: bool) -> str:
    opening, closing = _CONFIRM[lang]
    pieces = _localized_facts(facts[:3], lang)
    if place:
        pieces += _localized_facts([place], lang)
    if depth:
        pieces.append(_WATER_DEPTH_TEXT[lang].format(depth))
    if rising:
        pieces += _localized_facts(["rising"], lang)
    if not pieces:
        return f"{opening}. {closing}"
    sep = "، " if lang == "ar" else ", "
    return f"{opening}: {sep.join(pieces[:5])}. {closing}"


def failed_audio_fields(ui_language: str) -> dict:
    """Fields for a voice note the AI could not process: ai_summary (English) and confirmation_message (UI language)."""
    lang = ui_language if ui_language in LANGS else "en"
    return {
        "ai_summary": "Voice note - needs human review (AI unavailable)",
        "confirmation_message": _RECEIVED_VOICE[lang],
    }
