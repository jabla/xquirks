"""ZHA's cluster classes a quirk file builds on (its manifest entry's inherits), checked inside Home Assistant.

The guard (guard.py) reads a file without running it. It lets a file that takes devices over from ZHA's own quirk
build on a class of that quirk which the file's manifest entry names, as the one base of one class of the file. It
cannot see that class. So before the change list offers such a file, and again before it is written, this check
looks each named module and class up among the modules ZHA has loaded (the file's import finds the same ones when ZHA
loads it) and refuses the file the way the guard does, with a reason, unless each one is:

- a module of the installed zhaquirks package (its file lies in the package's folder) that holds the class;
- a class defined in zhaquirks, a subclass of zhaquirks' CustomCluster (zigpy.quirks.CustomCluster is only a
  deprecated alias of it), kept out of zigpy's registry (_skip_registry True, which the file's class inherits:
  zigpy would build every device's cluster of that id from the file's class otherwise) and with a cluster id
  (the file's class sets none);
- a class whose definitions the file's class neither repeats nor misses. The members its nested AttributeDefs,
  ServerCommandDefs and ClientCommandDefs add take no name and no id of ZHA's class (ZHA's code reads its own
  definitions). A command name counts in both command classes: zigpy finds `cluster.<name>` in ClientCommandDefs,
  then in ServerCommandDefs, so a client command of the file named like a server command of ZHA's class would change
  what ZHA's own entities send (`cluster.on()`). The file's two command classes share no name either. Every name the
  file reads through its class (Own.AttributeDefs.x) without defining it is one of ZHA's class: a name ZHA renamed
  would make the import of the file fail when ZHA loads it.

The check imports nothing. Importing a zhaquirks module registers its quirks (zhaquirks' setup imports every module of
the package when ZHA starts with its quirks): an import from here before that, or with ZHA's quirks switched off, would
register them where ZHA has not (and in another order). A module ZHA has not loaded, or is loading, raises NotLoaded:
the file waits for the check, which runs again on the next refresh. The check runs in an executor job, never in the
event loop. Any other error of it refuses the file, none ends the check.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .guard import CLASS_NAME, INHERIT_MODULE, INHERIT_NESTED, Inheritance

# A reason is cut to this length: an error message comes from code outside this integration.
MAX_REASON = 200
# The command classes of a cluster class: zigpy finds a command by its name in both (Cluster.__getattr__).
COMMAND_CLASSES = ("ServerCommandDefs", "ClientCommandDefs")


class NotLoaded(Exception):
    """A module the check needs that ZHA has not loaded (yet), or is loading: no verdict, the check runs again later."""


def loaded(name: str) -> Any:
    """The module of this name as ZHA loaded it, never imported from here: NotLoaded while it is missing or its import
    runs (a partly run module lacks names it will have)."""
    module = sys.modules.get(name)
    if module is None or getattr(getattr(module, "__spec__", None), "_initializing", False):
        raise NotLoaded(f"ZHA has not loaded {name} yet")
    return module


def problems(inherits: frozenset[tuple[str, str]], uses: dict[tuple[str, str], Inheritance]) -> tuple[str, ...]:
    """Why a file that builds on these classes of ZHA's (what it does with each: guard.inheritances) is refused,
    one reason per line; empty when every class fits."""
    out = []
    for module, name in sorted(inherits):
        try:
            reasons = _reasons(module, name, uses.get((module, name)))
        except NotLoaded:
            raise
        except Exception as err:  # noqa: BLE001 - a class this check cannot judge refuses the file, it never ends the check
            reasons = [f"cannot be checked ({error_text(err)})"]
        out += [f"ZHA's class {name} ({module}): {reason}" for reason in reasons]
    return tuple(out)


def error_text(err: BaseException) -> str:
    """An error of code outside this integration as part of a reason: its type and text, cut to MAX_REASON."""
    text = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 3] + "..."


def _reasons(module_name: str, name: str, use: Inheritance | None) -> list[str]:
    if not (INHERIT_MODULE.fullmatch(module_name) and CLASS_NAME.fullmatch(name)):
        return ["no zhaquirks module and class name"]
    zhaquirks, clusters, module = loaded("zhaquirks"), loaded("zhaquirks.clusters"), loaded(module_name)
    # zigpy registers no quirk: ZHA has imported it before it loads any quirk.
    from zigpy.zcl.foundation import ZCLAttributeDef, ZCLCommandDef  # noqa: PLC0415

    custom_cluster = clusters.CustomCluster
    file = getattr(module, "__file__", None)
    if module.__name__ != module_name or not isinstance(file, str) or not Path(file).resolve().is_relative_to(Path(zhaquirks.__file__).resolve().parent):
        return ["no module of the installed zhaquirks"]
    # The module's own names, without a module __getattr__ (which could run code); the file's import finds them too.
    if name not in vars(module):
        return [f"{module_name} has no {name}"]
    cls = vars(module)[name]
    if not isinstance(cls, type):
        return ["not a class"]
    if not issubclass(cls, custom_cluster):
        return ["no zhaquirks cluster class (CustomCluster)"]
    if getattr(cls, "_skip_registry", False) is not True:
        return ["not kept out of zigpy's registry (_skip_registry): zigpy would build every device's cluster of its id from the file's class"]
    defined_in = getattr(cls, "__module__", None)
    if not (isinstance(defined_in, str) and (defined_in == "zhaquirks" or defined_in.startswith("zhaquirks."))):
        return ["not defined in zhaquirks"]
    cluster_id = getattr(cls, "cluster_id", None)
    if not (isinstance(cluster_id, int) and not isinstance(cluster_id, bool) and 0 <= cluster_id <= 0xFFFF):
        return ["no cluster id"]
    if use is None:
        return ["no class of the file builds on it"]
    kinds = {"AttributeDefs": ZCLAttributeDef, "ServerCommandDefs": ZCLCommandDef, "ClientCommandDefs": ZCLCommandDef}
    theirs_by = {nested: _definitions(getattr(cls, nested, None), kinds[nested]) for nested in INHERIT_NESTED}
    out = []
    for nested in INHERIT_NESTED:
        theirs = theirs_by[nested]
        ids = {identifier: member for member, identifier in theirs.items()}
        # A command name of ZHA's in the other command class: zigpy finds both by the name.
        other = next((theirs_by[name] for name in COMMAND_CLASSES if nested in COMMAND_CLASSES and name != nested), {})
        for member, identifier in sorted(use.defines.get(nested, {}).items()):
            if member in theirs:
                out.append(f"the file's {use.own}.{nested} defines {member}, which ZHA's class has")
            elif member in other:
                out.append(f"the file's {use.own}.{nested} defines {member}, a command of ZHA's class in its other command class (zigpy finds both by the name)")
            elif identifier in ids:
                out.append(f"the file's {use.own}.{nested} defines {member} with the id 0x{identifier:04X} of ZHA's {ids[identifier]}")
        out += [f"the file reads {use.own}.{nested}.{member}, which neither its class nor ZHA's defines" for member in sorted(use.reads.get(nested, ())) if member not in theirs]
    both = sorted(set(use.defines.get(COMMAND_CLASSES[0], {})) & set(use.defines.get(COMMAND_CLASSES[1], {})))
    out += [f"the file's {use.own} defines the command {member} in both ServerCommandDefs and ClientCommandDefs" for member in both]
    return out


def _definitions(defs: Any, kind: type) -> dict[str, int]:
    """The definitions of kind a nested class of a cluster class holds (its own and those of its bases): name -> id."""
    if not isinstance(defs, type):
        return {}
    found = {}
    for member in dir(defs):
        if not member.startswith("_") and isinstance(value := getattr(defs, member, None), kind):
            found[member] = int(value.id)
    return found
