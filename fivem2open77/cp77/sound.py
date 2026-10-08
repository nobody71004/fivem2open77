"""Engine sound of a converted car.

Cyberpunk picks a vehicle's engine, wheel and bump sounds from two TweakDB strings on the
vehicle record: ``player_audio_resource`` (driven by a player) and ``traffic_audio_resource``
(driven by an NPC), both names of sound setups in the game's cooked audio metadata, e.g.
``v_car_porsche_911_turbo`` / ``v_car_porsche_911_turbo_traffic``.  A converted record inherits
the donor's pair through ``$base``; setting the pair gives it another car's sound.

GTA names the sound a car uses with ``audioNameHash`` in vehicles.meta (a GTA vehicle such as
ELEGY, or the name of an add-on sound pack).  ``resolve`` maps that, or the GTA vehicle class,
to the closest Cyberpunk sound.  The game's sounds are referenced by name only.
"""
from __future__ import annotations

import json
import os
import re

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cp_vehicle_audio.json")
with open(_DATA, "r", encoding="utf-8") as _f:
    CATALOGUE: dict = json.load(_f)["sounds"]

# character -> the Cyberpunk sound used for it
BY_CHARACTER = {
    "hyper": "v_car_herrera_outlaw",
    "super": "v_car_quadra_turbo_r",
    "electric": "v_car_rayfield_aerondight",
    "flat6": "v_car_porsche_911_turbo",
    "sport": "v_car_porsche_911_turbo",
    "jdm": "v_car_mizutani_shion",
    "muscle": "v_car_quadra_type_66",
    "classic": "v_car_villefort_alvarado",
    "luxury": "v_car_chevalier_thrax",
    "coupe": "v_car_villefort_deleon",
    "sedan": "v_car_villefort_cortes",
    "compact": "v_car_archer_hella",
    "micro": "v_car_makigai_maimai",
    "suv": "v_car_mahir_supron",
    "offroad": "v_car_thorton_mackinaw",
    "van": "v_car_villefort_columbus",
    "truck": "v_car_kaukaz_bratsk",
    "police": "v_car_villefort_cortes_police",
    "police_suv": "v_car_thorton_merrimac_police",
    "military": "v_car_militech_hellhound",
    "bike": "v_mbike_yaiba_asmx_kusanagi",
    "chopper": "v_mbike_arch_nazare_chopper",
}

# GTA V sound names (audioNameHash = a vanilla vehicle) by character
_GTA = {
    "hyper": "ADDER T20 OSIRIS ENTITYXF ENTITY2 ENTITY3 NERO NERO2 TYRANT KRIEGER EMERUS FMJ SHEAVA PROTOTIPO "
             "VAGNER XA21 AUTARCH DEVESTE THRAX ZENO LE7B SC1 FURIA IGNUS IGNUS2 S80 TIGON VISIONE TURISMOR "
             "TURISMO3 PFISTER811 TORERO2 LM87 CORSITA",
    "super": "ZENTORNO INFERNUS CHEETAH TEMPESTA VACCA REAPER ITALIGTB ITALIGTB2 PENETRATOR BULLET GP1 TAIPAN "
             "ZORRUSSO BANSHEE2 VOLTIC2 FURIA ITALIRSX ISSI8 OMNISEGT",
    "electric": "VOLTIC CYCLONE CYCLONE2 RAIDEN NEON KHAMELION TEZERACT IMORGON DILETTANTE DILETTANTE2 SURGE "
                "IWAGEN VIRTUE CADDY CADDY2 CADDY3 AIRTUG",
    "flat6": "COMET2 COMET3 COMET4 COMET5 COMET6 COMET7 GROWLER",
    "jdm": "ELEGY ELEGY2 SULTAN SULTAN2 SULTAN3 SULTANRS JESTER JESTER2 JESTER3 JESTER4 JESTER5 FUTO FUTO2 "
           "ZR350 CALICO EUROS REMUS KURUMA KURUMA2 RT3000 VECTRE PREVION SUGOI PENUMBRA PENUMBRA2 BLISTA2 "
           "BLISTA3 KANJO KANJOSJ POSTLUDE HARDY",
    "sport": "CARBONIZZARE MASSACRO MASSACRO2 NINEF NINEF2 FUROREGT FELTZER2 SCHAFTER3 SCHLAGEN SEVEN70 SPECTER "
             "SPECTER2 PARAGON PARAGON2 PARIAH NEO DRAFTER ITALIGTO KOMODA LOCUST RAPIDGT RAPIDGT2 SURANO "
             "VERLIERER2 BESTIAGTS OMNIS LYNX ALPHA STREITER SENTINEL3 SENTINEL4 CYPHER VSTR TENF TENF2 SM722 "
             "COQUETTE4 RUSTON CORSITA RAIDEN STINGERTT",
    "muscle": "DOMINATOR DOMINATOR2 DOMINATOR3 DOMINATOR4 DOMINATOR5 DOMINATOR6 DOMINATOR7 DOMINATOR8 DOMINATOR9 "
              "GAUNTLET GAUNTLET2 GAUNTLET3 GAUNTLET4 GAUNTLET5 DUKES DUKES2 DUKES3 SABREGT SABREGT2 VIGERO "
              "VIGERO2 VIGERO3 RUINER RUINER2 BLADE BUCCANEER BUCCANEER2 CHINO CHINO2 FACTION FACTION2 FACTION3 "
              "HOTKNIFE HERMES HUSTLER IMPALER IMPERATOR MOONBEAM NIGHTSHADE PHOENIX PICADOR RATLOADER SLAMVAN "
              "STALION STALION2 TAMPA TAMPA2 TULIP TULIP2 VAMOS VIRGO VIRGO2 VIRGO3 VOODOO VOODOO2 YOSEMITE "
              "ELLIE DEVIANT CLIQUE BANSHEE COQUETTE COQUETTE3 BUFFALO BUFFALO2 BUFFALO3 BUFFALO4 BUFFALO5 "
              "GREENWOOD ARBITERGT TAHOMA BROADWAY EUDORA WEEVIL2 GAUNTLET",
    "classic": "GT500 STINGER STINGERGT TORNADO TORNADO2 TORNADO3 TORNADO4 TORNADO5 TORNADO6 JB700 JB7002 MAMBA "
               "MONROE Z190 CHEBUREK RETINUE RETINUE2 MICHELLI TURISMO2 INFERNUS2 CHEETAH2 TORERO RAPIDGT3 "
               "SAVESTRA VISERIS ARDENT CASCO FAGALOA PEYOTE PEYOTE2 PEYOTE3 MANANA MANANA2 BTYPE BTYPE2 BTYPE3 "
               "ZTYPE SWINGER PIGALLE COQUETTE2 FELTZER3 DYNASTY NEBULA STROMBERG DELUXO TOREADOR ZION3 "
               "GLENDALE",
    "luxury": "SCHAFTER2 SCHAFTER4 SCHAFTER5 SCHAFTER6 ORACLE ORACLE2 TAILGATER TAILGATER2 COGNOSCENTI "
              "COGNOSCENTI2 COG55 COG552 SUPERD WINDSOR WINDSOR2 EXEMPLAR FELON FELON2 F620 JACKAL ZION ZION2 "
              "SENTINEL SENTINEL2 CINQUEMILA DEITY RHINEHART STAFFORD JUBILEE",
    "sedan": "PREMIER PRIMO PRIMO2 STANIER STRATUM WASHINGTON INGOT ASEA ASEA2 ASTEROPE FUGITIVE EMPEROR "
             "EMPEROR2 EMPEROR3 GLENDALE2 INTRUDER REGINA ROMERO STRETCH WARRENER WARRENER2 TAXI",
    "micro": "PANTO ISSI2 ISSI3 BRIOSO BRIOSO2 BRIOSO3 WEEVIL",
    "compact": "BLISTA PRAIRIE RHAPSODY ASBO CLUB",
    "suv": "BALLER BALLER2 BALLER3 BALLER4 BALLER5 BALLER6 BALLER7 BALLER8 CAVALCADE CAVALCADE2 CAVALCADE3 "
           "GRANGER GRANGER2 HUNTLEY LANDSTALKER LANDSTALKER2 DUBSTA DUBSTA2 XLS XLS2 ROCOTO SERRANO "
           "CONTENDER NOVAK REBLA TOROS SEMINOLE SEMINOLE2 PATRIOT PATRIOT2 GRESLEY FQ2 BJXL MESA MESA2 "
           "HABANERO ASTRON SQUADDIE VIVANITE",
    "offroad": "SANDKING SANDKING2 BISON BISON2 BISON3 REBEL REBEL2 BOBCATXL SADLER SADLER2 KAMACHO CARACARA "
               "CARACARA2 GUARDIAN DUBSTA3 TROPHYTRUCK TROPHYTRUCK2 BRAWLER MESA3 RIATA EVERON YOSEMITE3 "
               "DLOADER BODHI2 KALAHARI BIFTA DRAUGUR HELLION WINKY VAGRANT OUTLAW ZHABA TERMINUS",
    "van": "BURRITO BURRITO2 BURRITO3 BURRITO4 BURRITO5 SPEEDO SPEEDO2 SPEEDO4 YOUGA YOUGA2 YOUGA3 YOUGA4 "
           "RUMPO RUMPO2 RUMPO3 PONY PONY2 MINIVAN MINIVAN2 SURFER SURFER2 JOURNEY JOURNEY2 CAMPER GBURRITO "
           "GBURRITO2 PARADISE AMBULANCE",
    "truck": "PHANTOM PHANTOM2 PHANTOM3 HAULER HAULER2 PACKER MULE MULE2 MULE3 MULE4 POUNDER POUNDER2 BENSON "
             "FLATBED BIFF MIXER MIXER2 TIPTRUCK TIPTRUCK2 DUMP RUBBLE TRASH TRASH2 STOCKADE BUS COACH AIRBUS "
             "TOURBUS RENTALBUS FIRETRUK BOXVILLE BOXVILLE2 BOXVILLE3 BOXVILLE4",
    "police": "POLICE POLICE2 POLICE3 POLICE4 POLICE5 POLICEOLD1 POLICEOLD2 POLICET SHERIFF",
    "police_suv": "FBI FBI2 SHERIFF2 PRANGER POLICEB RIOT RIOT2",
    "military": "BARRAGE INSURGENT INSURGENT2 INSURGENT3 NIGHTSHARK MENACER HALFTRACK APC KHANJALI RHINO "
                "CRUSADER TECHNICAL TECHNICAL2 TECHNICAL3 BARRACKS BARRACKS2 BARRACKS3",
    "bike": "BATI BATI2 AKUMA CARBONRS DOUBLE HAKUCHOU HAKUCHOU2 PCJ RUFFIAN VADER NEMESIS LECTRO DEFILER "
            "SHOTARO FCR FCR2 DIABLOUS DIABLOUS2 VORTEX ESSKEY ENDURO BF400 SANCHEZ SANCHEZ2 MANCHEZ "
            "MANCHEZ2 REEVER SHINOBI POWERSURGE",
    "chopper": "DAEMON DAEMON2 HEXER INNOVATION SOVEREIGN NIGHTBLADE ZOMBIEA ZOMBIEB CHIMERA AVARUS SANCTUS "
               "WOLFSBANE CLIFFHANGER GARGOYLE BAGGER RATBIKE FAGGIO FAGGIO2 FAGGIO3 THRUST VINDICATOR",
}
GTA_SOUND = {}
for _char, _names in _GTA.items():
    for _n in _names.split():
        GTA_SOUND.setdefault(_n, _char)

# add-on sound packs: their names usually say what they are ("lambov10", "rb26dett", "s55b30").
# Brand and model names match anywhere; engine codes at the start of a word (between
# underscores, digits may follow); "=code" only as a whole word.
_KW = [
    ("electric", "tesla electric taycan rimac lucid etron ioniq plaid", "=ev =eqs =leaf"),
    ("hyper", "bugatti chiron veyron koenig agera jesko pagani zonda huayra laferrari valkyrie", "senna =p1 918 w16"),
    ("super", "lambo aventador huracan murci gallardo ferrari mclaren",
     "f12 812 458 488 =f8 sf90 720s 765lt r8 v12 v10 lfa"),
    ("flat6", "porsche cayman carrera flat6 boxer", "gt3 gt2 911 992 991 997"),
    ("jdm", "2jz 1jz sr20 4g63 skyline supra silvia civic honda toyota nissan mazda subaru mitsu jdm wrx",
     "rb25 rb26 ej20 ej25 k20 k24 b16 b18 13b 20b rx7 rx8 vq35 vq37 r32 r33 r34 r35 gtr s13 s14 s15 evo =sti nsx"),
    ("muscle", "hemi hellcat coyote mustang camaro charger challenger corvette vette viper shelby trackhawk muscle",
     "demon ls1 ls2 ls3 ls6 ls7 ls9 lt1 lt4 lt5 gt500 srt v8"),
    ("luxury", "bmw mercedes bentley rolls maybach lexus cadillac",
     "s55 s58 s63 n54 n55 b58 m3 m4 m5 m6 m8 amg m177 m178 =audi rs3 rs4 rs5 rs6 rs7"),
    ("offroad", "diesel cummins duramax powerstroke raptor tundra offroad", "trx f150 =ram 4x4"),
    ("truck", "truck peterbilt kenworth scania", "=semi"),
    ("bike", "kawasaki ducati yamaha hayabusa motorbike superbike", "cbr =r1 =r1m =bike"),
    ("chopper", "harley chopper", "=cruiser"),
]


def _kw_pattern(anywhere: str, word: str):
    alts = [re.escape(w) for w in anywhere.split()]
    for w in word.split():
        alts.append(rf"(?:^|_){re.escape(w[1:])}(?:_|$)" if w.startswith("=") else rf"(?:^|_){re.escape(w)}")
    return re.compile("|".join(alts))


KEYWORDS = [(char, _kw_pattern(anywhere, word)) for char, anywhere, word in _KW]

BY_CLASS = {
    "VC_SUPER": "super", "VC_SPORT": "sport", "VC_SPORT_CLASSIC": "classic", "VC_MUSCLE": "muscle",
    "VC_COUPE": "coupe", "VC_SEDAN": "sedan", "VC_COMPACT": "compact", "VC_SUV": "suv",
    "VC_OFF_ROAD": "offroad", "VC_VAN": "van", "VC_COMMERCIAL": "truck", "VC_INDUSTRIAL": "truck",
    "VC_UTILITY": "truck", "VC_SERVICE": "van", "VC_EMERGENCY": "police", "VC_MILITARY": "military",
    "VC_MOTORCYCLE": "bike",
}


def gta_audio_name(vehicle) -> str:
    return (getattr(vehicle, "audio_name", "") or "").strip()


def character_of(audio_name: str, gta_class: str = "") -> tuple[str | None, str]:
    """-> (character, why)."""
    a = (audio_name or "").strip()
    if a:
        up = a.upper()
        if up in GTA_SOUND:
            return GTA_SOUND[up], f"GTA sound {up}"
        low = re.sub(r"[^a-z0-9]+", "_", a.lower())
        for char, rx in KEYWORDS:
            if rx.search(low):
                return char, f"sound pack '{a}'"
    c = (gta_class or "").upper()
    if c in BY_CLASS:
        return BY_CLASS[c], f"class {c}" + (f" (sound '{a}' unknown)" if a else "")
    return None, "no GTA sound or class to go by"


def describe(name: str) -> dict:
    s = CATALOGUE[name]
    return {"player": name, "traffic": s["traffic"], "label": s["label"], "kind": s["kind"]}


def resolve(setting: str, vehicle, donor_sound: str) -> dict:
    """Engine sound for a car.

    setting: "auto" (from GTA's audioNameHash / class), "donor" (inherit the donor's) or a sound name
    from CATALOGUE.  Returns {"player", "traffic", "label", "kind", "source", "override": bool};
    override is False when the result is the donor's own sound (nothing to write)."""
    setting = (setting or "auto").strip()
    if setting == "donor":
        out, source = describe(donor_sound), "donor"
    elif setting == "auto":
        char, why = character_of(gta_audio_name(vehicle), getattr(vehicle, "gta_class", ""))
        if char is None:
            out, source = describe(donor_sound), f"donor ({why})"
        else:
            out, source = describe(BY_CHARACTER[char]), f"{why} -> {char}"
    else:
        if setting not in CATALOGUE:
            raise ValueError(f"unknown engine sound '{setting}' (see the sound list)")
        out, source = describe(setting), "profile"
    out["source"] = source
    out["override"] = out["player"] != donor_sound
    return out


def choices() -> list:
    """Sound list for the UI, cars first."""
    order = {"super": 0, "sport": 1, "jdm": 2, "muscle": 3, "electric": 4, "luxury": 5, "classic": 6, "sedan": 7,
             "compact": 8, "micro": 9, "suv": 10, "offroad": 11, "pickup": 12, "van": 13, "truck": 14,
             "police": 15, "military": 16, "bike": 17}
    return sorted(({"name": k, **v} for k, v in CATALOGUE.items()),
                  key=lambda s: (order.get(s["kind"], 99), s["label"]))
