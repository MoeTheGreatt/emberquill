"""In-game cheat toggles, driven from the options menu.

Every cheat is a LIVE OVERRIDE read at the point of use -- never a write to
save data. That is what makes "off means off": flipping a toggle off makes the
game read its real money, items, PP and encounter rates again on the very next
check. The only things that persist are results already banked (a shiny that
was caught stays shiny, levels gained stay gained), which no amount of care can
undo.

The flags live in the eight unused bytes at ``SaveBlock2 + 0x090``
(``filler_90``), so no existing field moves and saves stay compatible in both
directions.

Requires :func:`decomp.enable_scrolling_options`: fifteen extra rows do not fit
a window that already holds exactly seven.
"""

from __future__ import annotations

import re
from pathlib import Path

from .decomp import RtlPatchError, _edit

# (menu constant, save field, label symbol, English label, value count)
# Value count 2 = an on/off toggle. The EXP row cycles three ways and the
# species picker walks the 386 real species (the name table has 412 entries,
# the rest being "??????????" placeholders that must stay unreachable).
CHEATS = (
    ("MENUITEM_CHEAT_WALLS",    "walkThroughWalls", "gText_Cheat_Walls",    "WALK THRU WALLS", 2),
    ("MENUITEM_CHEAT_MONEY",    "infiniteMoney",    "gText_Cheat_Money",    "INFINITE MONEY",  2),
    ("MENUITEM_CHEAT_ITEMS",    "infiniteItems",    "gText_Cheat_Items",    "INFINITE ITEMS",  2),
    ("MENUITEM_CHEAT_CATCH",    "alwaysCatch",      "gText_Cheat_Catch",    "ALWAYS CATCH",    2),
    ("MENUITEM_CHEAT_NOENC",    "noWildEncounters", "gText_Cheat_NoEnc",    "NO ENCOUNTERS",   2),
    ("MENUITEM_CHEAT_FORCEENC", "forceEncounters",  "gText_Cheat_ForceEnc", "FORCE ENCOUNTER", 2),
    ("MENUITEM_CHEAT_SHINY",    "shinyBoost",       "gText_Cheat_Shiny",    "SHINY WILD",      2),
    ("MENUITEM_CHEAT_HATCH",    "instantHatch",     "gText_Cheat_Hatch",    "INSTANT HATCH",   2),
    ("MENUITEM_CHEAT_MISS",     "neverMiss",        "gText_Cheat_Miss",     "NEVER MISS",      2),
    ("MENUITEM_CHEAT_OHKO",     "oneHitKO",         "gText_Cheat_Ohko",     "ONE HIT KO",      2),
    ("MENUITEM_CHEAT_PP",       "infinitePP",       "gText_Cheat_Pp",       "INFINITE PP",     2),
    ("MENUITEM_CHEAT_NOFAINT",  "noFaint",          "gText_Cheat_NoFaint",  "NO FAINT",        2),
    ("MENUITEM_CHEAT_EXP",      "expMultiplier",    "gText_Cheat_Exp",      "EXP BOOST",       3),
    ("MENUITEM_CHEAT_SPAWNON",  "spawnerOn",        "gText_Cheat_SpawnOn",  "WILD SPAWNER",    2),
    ("MENUITEM_CHEAT_SPECIES",  None,               "gText_Cheat_Species",  "SPAWN SPECIES",   386),
)

_STRUCT = """
// Cheat toggles, stored in SaveBlock2's unused filler_90 so that no existing
// field moves and saves stay compatible both ways. Read live at the point of
// use -- nothing here is ever applied TO save data, which is what lets a
// toggle be switched back off.
struct Cheats
{
    u32 walkThroughWalls:1;
    u32 infiniteMoney:1;
    u32 infiniteItems:1;
    u32 alwaysCatch:1;
    u32 noWildEncounters:1;
    u32 forceEncounters:1;
    u32 shinyBoost:1;
    u32 instantHatch:1;
    u32 neverMiss:1;
    u32 oneHitKO:1;
    u32 infinitePP:1;
    u32 noFaint:1;
    u32 expMultiplier:2;   // 0 = off, 1 = x2, 2 = x4
    u32 spawnerOn:1;
    u32 unusedFlags:17;
    u16 spawnerSpecies;    // 1..NUM_SPECIES, 0 = unset
    u16 unusedHalf;
};
"""


def _species_table(repo: Path) -> str:
    """A private copy of the ENGLISH species names, for the spawner picker.

    The species table itself is about to be replaced with Arabic, and the
    picker shows English names by choice: they are unambiguous, and at 60px for
    the longest ("WIGGLYTUFF") they fit the value column's 70px erase rect,
    which Arabic names would not be guaranteed to.

    This must therefore run BEFORE the translation pass rewrites the table.
    """
    path = repo / "src" / "data" / "text" / "species_names.h"
    text = path.read_text(encoding="utf-8")
    # English names start with an ASCII capital (or the "??????????" filler).
    # Once translated they start with charmap characters instead. Note the
    # table legitimately contains non-ASCII (NIDORAN's gender signs), so the
    # test has to look at the name openings, not the file's codepoint range.
    if not re.search(r'_\("[A-Z?]{3}', text):
        raise RtlPatchError(
            "species_names.h no longer holds English names -- enable_cheats "
            "must run before the translation pass")
    body = text[text.index("{") + 1:text.rindex("};")]
    return ("static const u8 sSpawnerSpeciesNames[][POKEMON_NAME_LENGTH + 1] = {"
            + body + "};\n\n")


def enable_cheats(repo: str | Path) -> list[str]:
    """Add the cheat toggles and the wild-Pokemon spawner."""
    repo = Path(repo)
    done: list[str] = []
    gh = repo / "include" / "global.h"
    om = repo / "src" / "option_menu.c"
    st = repo / "src" / "strings.c"
    sh = repo / "include" / "strings.h"

    if "struct Cheats" in gh.read_text(encoding="utf-8"):
        return ["already patched"]
    if "OPTIONS_VISIBLE_ROWS" not in om.read_text(encoding="utf-8"):
        raise RtlPatchError(
            "enable_scrolling_options must run first -- 15 extra rows do not "
            "fit a window that holds exactly 7")

    # -- storage ----------------------------------------------------------
    _edit(gh, "struct SaveBlock2\n{", _STRUCT + "\nstruct SaveBlock2\n{")
    _edit(gh, "    /*0x090*/ u8 filler_90[0x8];",
          "    /*0x090*/ struct Cheats cheats;  // was filler_90, same 8 bytes")
    done.append("global.h: cheat flags in filler_90 (8 bytes, no field moves)")

    # -- label + value strings -------------------------------------------
    # Injected in English and picked up by the normal translation pass, so the
    # labels get Arabic from the JSON like every other string, and land in the
    # fixed-column exclusion list with the rest of the option menu.
    decls = "".join(
        f'const u8 {sym}[] = _("{en}");\n' for _c, _f, sym, en, _n in CHEATS)
    decls += ('const u8 gText_Cheat_On[] = _("ON");\n'
              'const u8 gText_Cheat_Off[] = _("OFF");\n'
              'const u8 gText_Cheat_X2[] = _("x2");\n'
              'const u8 gText_Cheat_X4[] = _("x4");\n')
    externs = "".join(
        f"extern const u8 {sym}[];\n" for _c, _f, sym, _e, _n in CHEATS)
    externs += ("extern const u8 gText_Cheat_On[];\n"
                "extern const u8 gText_Cheat_Off[];\n"
                "extern const u8 gText_Cheat_X2[];\n"
                "extern const u8 gText_Cheat_X4[];\n")
    _edit(st, "const u8 gText_LevelUp_MaxHP[]", decls + "const u8 gText_LevelUp_MaxHP[]")
    _edit(sh, "extern const u8 gText_LevelUp_MaxHP[];",
          externs + "extern const u8 gText_LevelUp_MaxHP[];")
    done.append(f"strings: {len(CHEATS) + 4} label/value strings")

    # -- menu rows --------------------------------------------------------
    _edit(om, "    MENUITEM_CANCEL,\n    MENUITEM_COUNT",
          "".join(f"    {c},\n" for c, _f, _s, _e, _n in CHEATS)
          + "    MENUITEM_CANCEL,\n    MENUITEM_COUNT")
    _edit(om, "static const u16 sOptionMenuItemCounts[MENUITEM_COUNT] = {3, 2, 2, 2, 3, 10, 0};",
          "static const u16 sOptionMenuItemCounts[MENUITEM_COUNT] = {3, 2, 2, 2, 3, 10, "
          + ", ".join(str(n) for _c, _f, _s, _e, n in CHEATS) + ", 0};")
    _edit(om, "    [MENUITEM_CANCEL]      = gText_OptionMenuCancel,",
          "".join(f"    [{c}] = {s},\n" for c, _f, s, _e, _n in CHEATS)
          + "    [MENUITEM_CANCEL]      = gText_OptionMenuCancel,")
    done.append(f"option_menu.c: {len(CHEATS)} rows")

    # The picker's name table, plus the shared value tables.
    _edit(om, "static const u8 *const sTextSpeedOptions[] =",
          _species_table(repo)
          + "static const u8 *const sCheatToggleOptions[] = {gText_Cheat_Off, gText_Cheat_On};\n"
            "static const u8 *const sCheatExpOptions[] = {gText_Cheat_Off, gText_Cheat_X2, gText_Cheat_X4};\n\n"
            "static const u8 *const sTextSpeedOptions[] =")
    _edit(om, '#include "strings.h"',
          '#include "strings.h"\n#include "constants/pokemon.h"\n'
          '#include "constants/species.h"')
    done.append("option_menu.c: English species names + value tables")

    # Values for the new rows. The vanilla switch has a case per row; these
    # share three shapes, so they are handled where it fell through.
    _edit(om, "    default:\n        break;\n    }\n    PutWindowTilemap(1);",
          "    default:\n"
          "        if (selection == MENUITEM_CHEAT_EXP)\n"
          "            AddTextPrinterParameterized3(1, FONT_NORMAL, x, y, dst, -1, sCheatExpOptions[sOptionMenuPtr->option[selection]]);\n"
          "        else if (selection == MENUITEM_CHEAT_SPECIES)\n"
          "            AddTextPrinterParameterized3(1, FONT_NORMAL, x, y, dst, -1, sSpawnerSpeciesNames[sOptionMenuPtr->option[selection] + 1]);\n"
          "        else if (selection >= MENUITEM_CHEAT_WALLS && selection <= MENUITEM_CHEAT_SPAWNON)\n"
          "            AddTextPrinterParameterized3(1, FONT_NORMAL, x, y, dst, -1, sCheatToggleOptions[sOptionMenuPtr->option[selection]]);\n"
          "        break;\n    }\n    PutWindowTilemap(1);")
    done.append("option_menu.c: value drawing for the new rows")

    # -- load / save ------------------------------------------------------
    loads = "".join(
        f"    sOptionMenuPtr->option[{c}] = gSaveBlock2Ptr->cheats.{f};\n"
        for c, f, _s, _e, _n in CHEATS if f)
    loads += ("    sOptionMenuPtr->option[MENUITEM_CHEAT_SPECIES] =\n"
              "        gSaveBlock2Ptr->cheats.spawnerSpecies ? gSaveBlock2Ptr->cheats.spawnerSpecies - 1 : 0;\n")
    saves = "".join(
        f"    gSaveBlock2Ptr->cheats.{f} = sOptionMenuPtr->option[{c}];\n"
        for c, f, _s, _e, _n in CHEATS if f)
    saves += ("    gSaveBlock2Ptr->cheats.spawnerSpecies =\n"
              "        sOptionMenuPtr->option[MENUITEM_CHEAT_SPECIES] + 1;\n")
    _edit(om, "    sOptionMenuPtr->option[MENUITEM_TEXTSPEED] = gSaveBlock2Ptr->optionsTextSpeed;",
          "    sOptionMenuPtr->option[MENUITEM_TEXTSPEED] = gSaveBlock2Ptr->optionsTextSpeed;\n"
          + loads)
    _edit(om, "    gSaveBlock2Ptr->optionsTextSpeed = sOptionMenuPtr->option[MENUITEM_TEXTSPEED];",
          "    gSaveBlock2Ptr->optionsTextSpeed = sOptionMenuPtr->option[MENUITEM_TEXTSPEED];\n"
          + saves)
    done.append("option_menu.c: load/save the flags")

    # -- the hooks --------------------------------------------------------
    money = repo / "src" / "money.c"
    _edit(money, "bool8 IsEnoughMoney(u32 *moneyPtr, u32 cost)\n{",
          "bool8 IsEnoughMoney(u32 *moneyPtr, u32 cost)\n{\n"
          "    if (gSaveBlock2Ptr->cheats.infiniteMoney)\n        return TRUE;\n")
    _edit(money, "void RemoveMoney(u32 *moneyPtr, u32 toSub)\n{",
          "void RemoveMoney(u32 *moneyPtr, u32 toSub)\n{\n"
          "    if (gSaveBlock2Ptr->cheats.infiniteMoney)\n        return;\n")
    done.append("money.c: infinite money (never deducted, never overwritten)")

    _edit(repo / "src" / "item.c",
          "    if (itemId == ITEM_NONE)\n        return FALSE;\n",
          "    if (itemId == ITEM_NONE)\n        return FALSE;\n\n"
          "    // Report the removal without performing it, so the caller\n"
          "    // proceeds and the item survives.\n"
          "    if (gSaveBlock2Ptr->cheats.infiniteItems)\n        return TRUE;\n")
    done.append("item.c: infinite items")

    we = repo / "src" / "wild_encounter.c"
    _edit(we, "    encounterRate *= 16;",
          "    // No-encounters deliberately wins over force-encounters.\n"
          "    if (gSaveBlock2Ptr->cheats.noWildEncounters)\n        return FALSE;\n"
          "    if (gSaveBlock2Ptr->cheats.forceEncounters)\n        return TRUE;\n"
          "    encounterRate *= 16;")
    done.append("wild_encounter.c: no/forced encounters")

    # The spawner. Every wild path -- grass, surf, rock smash, all four rods --
    # funnels through GenerateWildMon, while scripted encounters (Mewtwo,
    # Snorlax, gifts) use other code entirely and stay untouched.
    #
    # The override must also route around the Unown branch below, which indexes
    # a letter table by map number and would read out of bounds anywhere
    # outside the Tanoby Ruins.
    _edit(we, "    ZeroEnemyPartyMons();\n    if (species != SPECIES_UNOWN)",
          "    ZeroEnemyPartyMons();\n"
          "    if (gSaveBlock2Ptr->cheats.spawnerOn\n"
          "     && gSaveBlock2Ptr->cheats.spawnerSpecies != SPECIES_NONE\n"
          "     && gSaveBlock2Ptr->cheats.spawnerSpecies <= NUM_SPECIES)\n"
          "    {\n"
          "        u32 shinyPersonality;\n"
          "        u8 tries = 0;\n"
          "\n"
          "        species = gSaveBlock2Ptr->cheats.spawnerSpecies;\n"
          "        if (!gSaveBlock2Ptr->cheats.shinyBoost)\n"
          "        {\n"
          "            CreateMonWithNature(&gEnemyParty[0], species, level, USE_RANDOM_IVS, Random() % NUM_NATURES);\n"
          "            return;\n"
          "        }\n"
          "        do\n"
          "        {\n"
          "            shinyPersonality = Random32();\n"
          "        } while (GET_SHINY_VALUE(gSaveBlock2Ptr->playerTrainerId[0]\n"
          "                              | (gSaveBlock2Ptr->playerTrainerId[1] << 8)\n"
          "                              | (gSaveBlock2Ptr->playerTrainerId[2] << 16)\n"
          "                              | (gSaveBlock2Ptr->playerTrainerId[3] << 24),\n"
          "                                shinyPersonality) >= SHINY_ODDS && ++tries < 64);\n"
          "        CreateMon(&gEnemyParty[0], species, level, USE_RANDOM_IVS, TRUE, shinyPersonality, OT_ID_PLAYER_ID, 0);\n"
          "        return;\n"
          "    }\n"
          "    if (species != SPECIES_UNOWN)")
    done.append("wild_encounter.c: spawner (all wild paths, Unown-safe)")

    bs = repo / "src" / "battle_script_commands.c"
    _edit(bs, "        if (odds > 254) // mon caught",
          "        if (gSaveBlock2Ptr->cheats.alwaysCatch)\n            odds = 255;\n"
          "        if (odds > 254) // mon caught")
    done.append("battle_script_commands.c: always catch")

    _edit(bs, "    s32 ppToDeduct = 1;",
          "    s32 ppToDeduct = gSaveBlock2Ptr->cheats.infinitePP ? 0 : 1;")
    done.append("battle_script_commands.c: infinite PP")

    _edit(bs, "        // final calculation\n        if ((Random() % 100 + 1) > calc)",
          "        // final calculation\n"
          "        if ((Random() % 100 + 1) > calc\n"
          "         && !(gSaveBlock2Ptr->cheats.neverMiss\n"
          "           && GetBattlerSide(gBattlerAttacker) == B_SIDE_PLAYER))")
    done.append("battle_script_commands.c: never miss (player's side only)")

    _edit(bs,
          "            else // hp goes down\n"
          "            {\n"
          "                if (gHitMarker & HITMARKER_SKIP_DMG_TRACK)",
          "            else // hp goes down\n"
          "            {\n"
          "                // One-hit KO hits the opposing side only; no-faint\n"
          "                // floors the player's side at 1 HP. Both adjust\n"
          "                // this hit only -- nothing is stored.\n"
          "                if (gSaveBlock2Ptr->cheats.oneHitKO\n"
          "                 && GetBattlerSide(gActiveBattler) == B_SIDE_OPPONENT)\n"
          "                    gBattleMoveDamage = gBattleMons[gActiveBattler].hp;\n"
          "                if (gSaveBlock2Ptr->cheats.noFaint\n"
          "                 && GetBattlerSide(gActiveBattler) == B_SIDE_PLAYER\n"
          "                 && gBattleMoveDamage >= gBattleMons[gActiveBattler].hp)\n"
          "                    gBattleMoveDamage = gBattleMons[gActiveBattler].hp - 1;\n"
          "                if (gHitMarker & HITMARKER_SKIP_DMG_TRACK)")
    done.append("battle_script_commands.c: one-hit KO + no faint")

    _edit(bs,
          "            calculatedExp = gSpeciesInfo[gBattleMons[gBattlerFainted].species].expYield * gBattleMons[gBattlerFainted].level / 7;",
          "            calculatedExp = gSpeciesInfo[gBattleMons[gBattlerFainted].species].expYield * gBattleMons[gBattlerFainted].level / 7;\n"
          "            if (gSaveBlock2Ptr->cheats.expMultiplier == 1)\n"
          "                calculatedExp *= 2;\n"
          "            else if (gSaveBlock2Ptr->cheats.expMultiplier == 2)\n"
          "                calculatedExp *= 4;")
    done.append("battle_script_commands.c: EXP multiplier")

    _edit(repo / "src" / "daycare.c",
          "                steps -= 1;\n"
          "                SetMonData(&gPlayerParty[i], MON_DATA_FRIENDSHIP, &steps);",
          "                if (gSaveBlock2Ptr->cheats.instantHatch)\n"
          "                    steps = 0;\n"
          "                else\n"
          "                    steps -= 1;\n"
          "                SetMonData(&gPlayerParty[i], MON_DATA_FRIENDSHIP, &steps);")
    done.append("daycare.c: instant hatch")

    # Placed after the stair-warp check so a legitimate warp still resolves.
    _edit(repo / "src" / "field_player_avatar.c",
          "    MoveCoords(direction, &x, &y);\n"
          "    return CheckForObjectEventCollision(playerObjEvent, x, y, direction, MapGridGetMetatileBehaviorAt(x, y));",
          "    MoveCoords(direction, &x, &y);\n"
          "    if (gSaveBlock2Ptr->cheats.walkThroughWalls)\n"
          "        return COLLISION_NONE;\n"
          "    return CheckForObjectEventCollision(playerObjEvent, x, y, direction, MapGridGetMetatileBehaviorAt(x, y));")
    done.append("field_player_avatar.c: walk through walls")

    return done
