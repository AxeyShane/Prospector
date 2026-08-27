"""Static checks on the Inno Setup script and the build script that drives it.

The .exe has to be compiled on Windows, so none of this can run ISCC. What it
can do is catch the mistakes that would otherwise only surface as a failed
build on the other machine: an undefined preprocessor variable, a directive
that does not exist in the targeted Inno version, a task referenced but never
declared, and -- the nastiest one -- a build script looking for an output file
under a different name than the script it just compiled produces.
"""

import re
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "installer"
ISS = INSTALLER / "prospector.iss"
PS1 = INSTALLER / "build_installer.ps1"
SPEC = INSTALLER / "prospector.spec"


def strip_comments(text: str) -> str:
    """Drop Inno comment lines.

    Without this, a checker fires on its own documentation -- the comment
    explaining *why* a 6.3-only directive is avoided contains that directive.
    A checker that cannot tell code from prose cries wolf until it is ignored.
    """
    return "\n".join(line for line in text.splitlines()
                      if not line.lstrip().startswith(";"))


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def iss_code(iss) -> str:
    return strip_comments(iss)


@pytest.fixture(scope="module")
def ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def test_the_installer_files_exist():
    for path in (ISS, PS1, SPEC, INSTALLER / "prospector_launcher.py"):
        assert path.exists(), f"missing {path.name}"


def test_every_preprocessor_reference_is_defined(iss):
    defined = set(re.findall(r"^#define\s+(\w+)", iss, re.M))
    defined |= set(re.findall(r"^\s*#define\s+(\w+)", iss, re.M))
    used = set(re.findall(r"\{#(?:emit\s+)?(\w+)", iss))
    # StringChange is a built-in ISPP function, not a variable.
    used -= {"StringChange"}
    missing = sorted(used - defined)
    assert not missing, f"used but never defined: {missing}"


def test_no_directives_newer_than_inno_6_2(iss_code):
    # These compile only on 6.3+. On 6.2.2 they fail with a message that does
    # not explain itself, on the other person's machine, after a long build.
    too_new = ("x64compatible", "x86compatible", "arm64compatible",
               "ArchitecturesAllowed=x64os", "WizardStyle=classic6")
    found = [t for t in too_new if t in iss_code]
    assert not found, f"needs Inno Setup newer than 6.2: {found}"


def test_architecture_directives_use_the_6_2_spelling(iss_code):
    assert "ArchitecturesAllowed=x64" in iss_code
    assert "ArchitecturesInstallIn64BitMode=x64" in iss_code


def test_it_installs_without_administrator_rights(iss_code):
    # A non-technical user on a work laptop often has no admin account. An
    # installer that demands one simply does not get installed.
    assert "PrivilegesRequired=lowest" in iss_code


def test_every_section_header_is_a_real_inno_section(iss):
    known = {"Setup", "Types", "Components", "Tasks", "Dirs", "Files", "Icons",
             "INI", "InstallDelete", "Languages", "Messages", "CustomMessages",
             "LangOptions", "Registry", "Run", "UninstallDelete",
             "UninstallRun", "Code", "ISPP"}
    sections = re.findall(r"^\[(\w+)\]", iss, re.M)
    unknown = sorted(set(sections) - known)
    assert not unknown, f"unknown section(s): {unknown}"
    assert "Setup" in sections and "Files" in sections


def test_referenced_tasks_are_declared(iss):
    declared = set()
    in_tasks = False
    for line in iss.splitlines():
        if line.startswith("["):
            in_tasks = line.strip() == "[Tasks]"
            continue
        if in_tasks:
            match = re.search(r'Name:\s*"([^"]+)"', line)
            if match:
                declared.add(match.group(1))

    referenced = set()
    for value in re.findall(r"Tasks:\s*([^\s;]+)", iss):
        referenced |= {v.strip() for v in value.split() if v.strip()}

    missing = sorted(referenced - declared)
    assert not missing, f"Tasks referenced but not declared: {missing}"


def test_the_appid_keeps_its_doubled_opening_brace(iss):
    # "{{GUID}" is how a literal brace is escaped. Written as "{GUID}" Inno
    # reads it as a constant and the AppId silently becomes something else,
    # which breaks upgrades and uninstalls.
    match = re.search(r"^AppId=(.+)$", iss, re.M)
    assert match, "no AppId"
    assert match.group(1).startswith("{{"), f"AppId not escaped: {match.group(1)}"


def _section(iss: str, name: str) -> str:
    """The body of one [Section], up to the next one.

    Scoped deliberately: an earlier version of this test scanned every Type:
    line in the whole file, so adding an [InstallDelete] section -- which only
    ever touches the program's own folder -- failed a test about user data.
    """
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[|\Z)", iss, re.M | re.S)
    return match.group(1) if match else ""


def test_the_users_work_is_not_deleted_on_uninstall(iss):
    # Removing the program must never remove the projects and spreadsheets.
    deletions = re.findall(r"^Type:.*Name:\s*\"([^\"]+)\"",
                           _section(iss, "UninstallDelete"), re.M)
    assert deletions, "no [UninstallDelete] section found"
    for target in deletions:
        assert "shared" in target or "engine" in target, \
            f"uninstall would delete user data: {target}"
    assert not any(t.rstrip("\\").endswith(".prospector") for t in deletions)


def test_upgrades_clear_the_previous_build_out_of_the_program_folder(iss):
    """Two PyInstaller generations in one folder shadow each other on import.

    The result is a crash that happens only on machines that upgraded, which is
    the hardest kind of bug to ever hear about.
    """
    body = _section(iss, "InstallDelete")
    assert body.strip(), "no [InstallDelete] section - upgrades leave stale files"
    targets = re.findall(r"^Type:.*Name:\s*\"([^\"]+)\"", body, re.M)
    assert any("_internal" in t for t in targets)
    # It must only ever touch the program's own folder.
    for target in targets:
        assert target.startswith("{app}"), f"InstallDelete outside {{app}}: {target}"


def test_a_running_copy_is_closed_before_it_is_overwritten(iss):
    """Locked files are skipped silently, producing a half-upgraded install."""
    assert re.search(r"^CloseApplications=yes", iss, re.M)
    assert re.search(r"^AppMutex=", iss, re.M)


def test_pascal_code_blocks_balance(iss):
    code = iss.split("[Code]", 1)[1] if "[Code]" in iss else ""
    if not code.strip():
        return
    # Rough but effective: `end` must not outnumber `begin` + `function`.
    begins = len(re.findall(r"\bbegin\b", code, re.I))
    ends = len(re.findall(r"\bend[;.]", code, re.I))
    assert ends <= begins + 2, f"unbalanced Pascal block: {begins} begin, {ends} end"


# ---------------------------------------------------------------------------
# The cross-checks -- where a mismatch means a build that "succeeds" and then
# cannot find what it built.
# ---------------------------------------------------------------------------

def test_the_build_script_passes_the_version_variable_the_iss_expects(iss, ps1):
    passed = set(re.findall(r"/D(\w+)=", ps1))
    version_defines = {d for d in re.findall(r"#ifndef\s+(\w+)", iss)}
    assert passed, "the build script passes no /D variables"
    assert passed <= version_defines | set(re.findall(r"#define\s+(\w+)", iss)), \
        f"build script passes {passed}, which the .iss never reads"
    assert "MyAppVersion" in passed


def test_the_output_filename_matches_what_the_build_script_looks_for(iss, ps1):
    base = re.search(r"^OutputBaseFilename=(.+)$", iss, re.M).group(1).strip()
    # OutputBaseFilename=ProspectorSetup-{#MyAppVersion}  ->  ProspectorSetup-
    prefix = base.split("{")[0]
    assert prefix, "could not read the output filename prefix"
    assert prefix in ps1, \
        f"the .iss produces '{prefix}...' but the build script never mentions it"


def test_the_source_folder_matches_what_pyinstaller_produces(iss, ps1):
    source = re.search(r'#define\s+MySourceDir\s+"([^"]+)"', iss).group(1)
    # "..\dist\Prospector" -> the folder name PyInstaller's COLLECT emits
    folder = source.replace("/", "\\").rstrip("\\").split("\\")[-1]
    spec = SPEC.read_text(encoding="utf-8")
    assert f'name="{folder}"' in spec, \
        f"the .iss reads from '{folder}' but the spec does not build that name"
    assert folder in ps1, "the build script does not check that folder"


def test_the_build_script_verifies_imports_before_freezing(ps1):
    # A missing dependency caught here costs thirty seconds. Caught after
    # shipping, it is a broken install on someone else's machine -- which is
    # exactly how the Flask bug reached the user last time.
    assert "does not import cleanly" in ps1
    # It must walk the package rather than name modules in a list. The earlier
    # version of this test only checked that a string was *present*, so it
    # happily passed while the script imported `prospector.localai`, a module
    # that had been deleted -- and the build failed on the user's machine.
    assert "walk_packages" in ps1, \
        "the import check must enumerate modules, not hardcode a list that rots"


# Suffixes that mean "a file on disk", not "a Python module".
FILE_SUFFIXES = {"ico", "spec", "iss", "exe", "py", "ps1", "bat", "db", "toml",
                 "md", "txt", "json", "log", "zip", "cfg", "yaml", "yml"}


def _module_exists(dotted: str) -> bool:
    src = Path(__file__).resolve().parents[1] / "src"
    rel = dotted.replace(".", "/")
    return (src / f"{rel}.py").exists() or (src / rel / "__init__.py").exists()


@pytest.mark.parametrize("path", [PS1, SPEC, INSTALLER / "prospector_launcher.py"])
def test_every_prospector_module_named_in_the_build_files_is_real(path):
    """The check the old test should have been.

    Any `prospector.something` written into a build file must resolve to a file
    on disk. Renaming a module and forgetting one of these is silent until the
    build runs on someone else's machine.
    """
    text = path.read_text(encoding="utf-8")
    named = set(re.findall(r"\bprospector\.[A-Za-z_][A-Za-z0-9_.]*", text))
    # Attribute access, not a module: prospector.__version__ / __path__.
    named = {n for n in named if "__" not in n}
    # Filenames, not modules: installer\prospector.spec, prospector.ico.
    named = {n for n in named if n.rsplit(".", 1)[-1] not in FILE_SUFFIXES}

    missing = sorted(n for n in named if not _module_exists(n))
    assert not missing, f"{path.name} names modules that do not exist: {missing}"


def test_the_build_script_fails_loudly_rather_than_half_succeeding(ps1):
    assert ps1.count("Fail ") >= 6, "not enough explicit failure paths"
    assert "$ErrorActionPreference" in ps1
    # It must tell the user where to get Inno Setup rather than just dying.
    assert "jrsoftware.org" in ps1
