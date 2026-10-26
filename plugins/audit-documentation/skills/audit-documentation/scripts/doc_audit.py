#!/usr/bin/env python3
"""
doc_audit.py — détection heuristique des écarts entre la documentation et le code.

Usage :
    python3 doc_audit.py <racine_du_projet> [--json] [--out FICHIER] [--max N]

Sans dépendance (stdlib Python >= 3.8). Lecture seule : le script ne modifie rien.

Ce que le script fait :
  - inventaire de la doc (points d'entrée, docs/, ADR, specs, instructions d'agents, CI) ;
  - détection de la stack et des « unités » de code (modules, packages, apps, services) ;
  - confrontation doc <-> code : chemins cités disparus, liens cassés, commandes citées
    inexistantes, unités absentes de la carte des modules, fichiers promis absents,
    endpoints sans description OpenAPI, chemins absolus vers l'extérieur du dépôt ;
  - fraîcheur (dates de vérification en masse, docs anciennes vs activité du code),
    duplication entre docs, specs sans statut, secrets suivis par git.

Chaque constat porte une gravité (bloquant / important / mineur) et une preuve.
Les constats sont des PISTES : l'agent doit vérifier chacun avant de le rapporter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

IGNORED_DIRS = {
    "node_modules", ".git", "dist", "build", "out", "target", "vendor", ".venv", "venv",
    "env", "__pycache__", "coverage", ".next", ".nuxt", ".turbo", ".cache", "Pods",
    ".gradle", "bin", "obj", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    ".tox", "site-packages", ".svelte-kit", ".expo", ".dart_tool", "storage", "tmp",
}
DOC_EXT = {".md", ".mdx", ".rst", ".adoc"}
CODE_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb", ".php", ".java",
    ".kt", ".kts", ".cs", ".rs", ".swift", ".dart", ".scala", ".ex", ".exs", ".vue",
    ".svelte", ".c", ".cc", ".cpp", ".h", ".hpp",
}
HISTORIC_HINTS = re.compile(
    r"(^|/)(adr|adrs|decisions|plans?|specs?|rfcs?|proposals?|archive[sd]?|history|historique|"
    r"changelog|migrations?|retours|postmortems?|meeting|notes|journal|qa-campaign)(/|$|\.)",
    re.I,
)
META_DOC = re.compile(r"(^|/)SKILL\.md$|(^|/)skills/[^/]+/references/", re.I)  # skills génériques : parlent d'autres projets
HISTORIC_MARKERS = re.compile(r"historique|historical|deprecated|obsol[eè]te|archived|superseded", re.I)
NO_GIT = False  # --no-git : saute les contrôles qui lisent l'historique (lents sur un dossier réseau / monté)
SEVERITY_ORDER = {"bloquant": 0, "important": 1, "mineur": 2}
WEIGHTS = {"bloquant": 15, "important": 5, "mineur": 1}
CAPS = {"bloquant": 60, "important": 40, "mineur": 10}

# --------------------------------------------------------------------------- utilitaires


def run(cmd, cwd, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def read(p: Path, limit=2_000_000) -> str:
    try:
        if p.stat().st_size > limit:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


class Repo:
    def __init__(self, root: Path, max_files=60000):
        self.root = root
        self.files: list[str] = []
        self.dirs: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info")]
            rd = Path(dirpath)
            r = "" if rd == root else rel(root, rd)
            if r:
                self.dirs.add(r)
            for f in filenames:
                self.files.append(f"{r}/{f}" if r else f)
                if len(self.files) >= max_files:
                    break
            if len(self.files) >= max_files:
                break
        self.all_paths = set(self.files) | self.dirs
        # index de suffixes (pour résoudre `modules/x/` ou `x.ts` cités sans chemin complet)
        self.suffixes: set[str] = set()
        for p in self.all_paths:
            parts = p.split("/")
            for i in range(len(parts)):
                self.suffixes.add("/".join(parts[i:]))
        self.is_git = (root / ".git").exists() and not NO_GIT
        self.top_level = {p.split("/")[0] for p in self.all_paths}
        self.dir_names = {d.split("/")[-1] for d in self.dirs}

    def history(self):
        """([(date, [fichiers])], {fichier: date du dernier commit}) — un seul `git log` par dépôt, mis en cache."""
        if getattr(self, "_history", None) is None:
            commits, last = [], {}
            out = run(["git", "log", "--no-merges", "--since=13.months.ago", "--format=@@%cs", "--name-only",
                       "-n", "1500"], self.root, timeout=150) if self.is_git else ""
            cur = None
            for line in out.splitlines():
                if line.startswith("@@"):
                    cur = (line[2:].strip(), [])
                    commits.append(cur)
                elif line.strip() and cur:
                    cur[1].append(line.strip())
                    last.setdefault(line.strip(), cur[0])
            self._history = (commits, last)
        return self._history

    def exists_loose(self, token: str, doc_dir: str) -> bool:
        t = token.strip()
        while t.startswith("./"):
            t = t[2:]
        t = t.strip("/")
        if not t:
            return True
        cands = {t}
        if doc_dir:
            cands.add(os.path.normpath(f"{doc_dir}/{token}").replace("\\", "/").strip("/"))
        if any(c in self.all_paths for c in cands):
            return True
        return t in self.suffixes


# --------------------------------------------------------------------------- écosystème (plusieurs dépôts)


_REPO_NAMES: dict = {}


def repo_name(path: Path) -> str:
    """Nom d'un dépôt : nom du remote git (stable d'un poste à l'autre), sinon nom du dossier."""
    if path in _REPO_NAMES:
        return _REPO_NAMES[path]
    _REPO_NAMES[path] = _repo_name(path)
    return _REPO_NAMES[path]


def _repo_name(path: Path) -> str:
    cfg = path / ".git" / "config"  # lecture directe : évite un appel git (lent sur un dossier monté)
    txt = read(cfg) if cfg.is_file() else ""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', txt)
    url = m.group(1) if m else (run(["git", "config", "--get", "remote.origin.url"], path).strip() if not txt else "")
    if url:
        n = re.split(r"[/:]", url.rstrip("/"))[-1]
        n = n[:-4] if n.endswith(".git") else n
        if n:
            return n
    return path.name


def has_known_ext(tok: str) -> bool:
    return Path(tok.rstrip("/")).suffix.lower() in CODE_EXT | DOC_EXT | {
        ".json", ".yml", ".yaml", ".toml", ".sql", ".prisma", ".sh", ".env", ".lock", ".html", ".css", ".scss"}


def split_sections(text: str):
    """[(titre, texte)] par titre markdown."""
    out, head, buf = [], "", []
    for line in text.splitlines():
        if re.match(r"^#{1,6} ", line):
            if buf:
                out.append((head, "\n".join(buf)))
            head, buf = line, [line]
        else:
            buf.append(line)
    if buf:
        out.append((head, "\n".join(buf)))
    return out


class Ecosystem:
    """Dépôts liés au dépôt audité : déclarés (CLI, .doc-audit.json) ou détectés (dossiers frères avec .git)."""

    def __init__(self, root: Path, related=None, hubs=None, auto=True, ecosystem_mode=False):
        self.root = root.resolve()
        self.paths: dict[str, Path] = {}     # nom (minuscule) -> chemin
        self.primary: dict[Path, str] = {}   # chemin -> nom affiché
        self._repos: dict[Path, Repo] = {}
        self._light: dict[Path, "Audit"] = {}
        self.ecosystem_mode = ecosystem_mode
        self.config_file = None
        self._register(repo_name(self.root), self.root)
        self._register(self.root.name, self.root)
        cfg = {}
        cf = self.root / ".doc-audit.json"
        if cf.exists():
            try:
                cfg = json.loads(read(cf)) or {}
                self.config_file = str(cf)
            except Exception:
                cfg = {}
        for name, p in (cfg.get("related") or {}).items():
            pp = (self.root / p).resolve() if not os.path.isabs(p) else Path(p)
            if pp.is_dir():
                self._register(name, pp)
                self._register(repo_name(pp), pp)
        for name, pp in (related or {}).items():
            if pp.is_dir():
                self._register(name, pp.resolve())
                self._register(repo_name(pp), pp.resolve())
        if auto and self.root.parent != self.root:
            # dossiers frères avec .git, gardés seulement s'ils sont cités (dans un sens ou dans l'autre)
            try:
                sibs = [s.resolve() for s in sorted(self.root.parent.iterdir())[:80]
                        if s.is_dir() and s.resolve() != self.root and (s / ".git").exists()]
            except Exception:
                sibs = []
            known = set(self.primary)
            texts = {p: self._doc_text(p) for p in [self.root] + [s for s in sibs if s not in known]}
            for _ in range(2):  # 2 passes : un hub cité par la racine fait entrer les dépôts qu'il cite
                for sib in sibs:
                    if sib in self.primary:
                        continue
                    sib_names = [repo_name(sib), sib.name]  # ordre : le nom du remote est le nom affiché
                    linked = [p for p in self.primary]
                    linked_names = {n for p in linked for n in self.aliases(p)} | {p.name for p in linked}
                    if any(self._mentions(texts[p], sib_names) for p in linked) or \
                            self._mentions(texts[sib], linked_names):
                        for n in sib_names:
                            self._register(n, sib)
        declared_hubs = list(hubs or []) + ([cfg["hub"]] if isinstance(cfg.get("hub"), str) else list(cfg.get("hubs") or []))
        self.hubs = [h.lower() for h in declared_hubs if h.lower() in self.paths] or self._guess_hubs()

    @staticmethod
    def _doc_text(path: Path, cap=4_000_000) -> str:
        buf, size = [], 0
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for f in filenames:
                if Path(f).suffix.lower() in DOC_EXT | {".mdc"}:
                    t = read(Path(dirpath) / f)
                    buf.append(t)
                    size += len(t)
                    if size > cap:
                        return "\n".join(buf)
        return "\n".join(buf)

    @staticmethod
    def _mentions(text: str, names) -> bool:
        return any(len(n) >= 3 and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I) for n in names)

    def _register(self, name: str, path: Path):
        k = name.lower()
        if k and k not in self.paths:
            self.paths[k] = path
        self.primary.setdefault(path, name)

    def keys(self):
        return set(self.paths)

    def label(self, key: str) -> str:
        return self.primary.get(self.paths[key], key)

    def aliases(self, path: Path):
        return {k for k, p in self.paths.items() if p == path}

    def unique_paths(self):
        return list(self.primary)

    def repo(self, key_or_path) -> Repo:
        p = key_or_path if isinstance(key_or_path, Path) else self.paths[key_or_path.lower()]
        if p not in self._repos:
            self._repos[p] = Repo(p)
        return self._repos[p]

    def light(self, path: Path) -> "Audit":
        """Audit minimal (docs + unités) d'un dépôt lié, sans contrôles."""
        if path not in self._light:
            a = Audit(path, eco=None, repo=self.repo(path))
            a.load_docs()
            a.detect_stack()
            a.discover_units()
            self._light[path] = a
        return self._light[path]

    def _guess_hubs(self):
        hubs = []
        for p in self.unique_paths():
            r = self.repo(p)
            docs = sum(1 for f in r.files if Path(f).suffix.lower() in DOC_EXT)
            code = sum(1 for f in r.files if Path(f).suffix in CODE_EXT)
            if docs >= 5 and code <= docs * 0.1:
                hubs.append(self.primary[p].lower() if self.primary[p].lower() in self.paths else
                            next(iter(self.aliases(p))))
        return hubs

    def is_hub(self, path: Path) -> bool:
        return any(self.paths.get(h) == path for h in self.hubs)

    def resolve_abs(self, p: str):
        """`D:/Projects/ride-api/src/x.ts` -> (clé du dépôt lié, 'src/x.ts') si un segment nomme un dépôt connu."""
        segs = [s for s in p.split("/") if s]
        for i, seg in enumerate(segs):
            k = seg.lower()
            if k in self.paths:
                return k, "/".join(segs[i + 1:])
        return None


# --------------------------------------------------------------------------- collecte


class Audit:
    def __init__(self, root: Path, eco: "Ecosystem | None" = None, repo: "Repo | None" = None):
        self.root = root
        self.eco = eco
        self.repo = repo or (eco.repo(root.resolve()) if eco else Repo(root))
        self.findings: list[dict] = []
        self.info: dict = {}
        self.docs: dict[str, str] = {}
        self.doc_kind: dict[str, str] = {}  # vivante | historique
        self.reported_gone: set[str] = set()  # "<parent>/<unité>" déjà signalés par la carte

    def add(self, severity, category, title, evidence, files=None, hint=None, scope="self"):
        # scope="hub" : constat sur ce que le hub dit de ce dépôt — affiché à part, hors score
        self.findings.append({
            "severity": severity, "category": category, "title": title,
            "evidence": evidence, "files": files or [], "hint": hint, "scope": scope,
        })

    # ---- inventaire --------------------------------------------------------
    def load_docs(self):
        for f in self.repo.files:
            p = Path(f)
            if p.suffix.lower() in DOC_EXT or p.name.upper() in {"README", "CONTRIBUTING", "CHANGELOG"}:
                txt = read(self.root / f)
                if not txt:
                    continue
                self.docs[f] = txt
                head = "\n".join(txt.splitlines()[:25])
                historic = bool(HISTORIC_HINTS.search(f)) or bool(HISTORIC_MARKERS.search(head))
                self.doc_kind[f] = "meta" if META_DOC.search(f) else "historique" if historic else "vivante"

    def inventory(self):
        root_files = {f for f in self.repo.files if "/" not in f}
        upper = {f.upper(): f for f in root_files}

        def has(*names):
            for n in names:
                for k, v in upper.items():
                    if k == n or k.startswith(n + "."):
                        return v
            return None

        entry = {
            "readme": has("README"),
            "contributing": has("CONTRIBUTING") or self._find_anywhere("CONTRIBUTING"),
            "changelog": has("CHANGELOG", "CHANGES", "HISTORY"),
            "license": has("LICENSE", "LICENCE", "COPYING"),
            "security": has("SECURITY") or self._find_anywhere("SECURITY"),
            "code_of_conduct": has("CODE_OF_CONDUCT"),
        }
        agent_files = [f for f in self.repo.files if re.search(
            r"(^|/)(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|copilot-instructions\.md)$", f)
            or f.startswith(".cursor/rules/") or f.startswith(".skills/") or f.startswith(".claude/skills/")]
        adr = sorted({f.rsplit("/", 1)[0] for f in self.docs if re.search(r"(^|/)(adr|adrs|decisions)/", f, re.I)})
        specs = [f for f in self.docs if re.search(r"(^|/)(specs?|plans?|rfcs?|proposals?)/", f, re.I)]
        self.info["entry_points"] = entry
        self.info["agent_instruction_files"] = len(agent_files)
        self.info["doc_files"] = len(self.docs)
        self.info["doc_files_living"] = sum(1 for k in self.doc_kind.values() if k == "vivante")
        self.info["doc_files_historic"] = sum(1 for k in self.doc_kind.values() if k != "vivante")
        self.info["adr_dirs"] = adr
        self.info["specs_plans"] = len(specs)

        index = has("INDEX")
        if not entry["readme"] and index:
            entry["readme"] = None
            self.add("mineur", "points-entree", "Pas de README à la racine",
                     f"{index} en tient lieu, mais GitHub et la plupart des outils affichent README.md.", [index])
        elif not entry["readme"]:
            self.add("bloquant", "points-entree", "Pas de README à la racine",
                     "Aucun fichier README* trouvé à la racine du dépôt.")
        else:
            txt = self.docs.get(entry["readme"], "")
            if not re.search(r"^#+ .*(install|setup|getting started|quick ?start|d[ée]marr|usage|utilisation|lancer|run)",
                             txt, re.I | re.M):
                self.add("mineur", "points-entree", "README sans section d'installation / démarrage",
                         f"Aucun titre de type « Installation / Démarrage / Usage » dans {entry['readme']}.",
                         [entry["readme"]])
        env_example = [f for f in self.repo.files if re.search(r"(^|/)\.env\.(example|sample|template|dist)$", f)]
        if env_example:
            names = {Path(e).name for e in env_example}
            if not any(any(n in t for n in names) for k, t in self.docs.items() if self.doc_kind[k] == "vivante"):
                self.add("mineur", "points-entree", "Fichier d'exemple d'environnement jamais cité",
                         f"{', '.join(env_example)} existe mais aucune doc vivante ne le mentionne.", env_example)
        for s in specs:
            head = "\n".join(self.docs[s].splitlines()[:30])
            if not re.search(r"\b(status|statut|state|état)\b", head, re.I):
                self.info.setdefault("_specs_no_status", []).append(s)
        nostat = self.info.pop("_specs_no_status", [])
        if len(nostat) >= 5:
            self.add("mineur", "organisation", "Specs / plans sans statut",
                     f"{len(nostat)}/{len(specs)} fichiers de specs/plans n'indiquent pas de statut "
                     "(livré, abandonné, en cours) dans leurs 30 premières lignes.", nostat[:10])

    def _find_anywhere(self, name):
        for f in self.repo.files:
            if Path(f).stem.upper() == name and Path(f).suffix.lower() in DOC_EXT | {""}:
                return f
        return None

    # ---- stack et unités ---------------------------------------------------
    def detect_stack(self):
        stack, frameworks = set(), set()
        deps_text = ""
        manifests = [f for f in self.repo.files if Path(f).name in {
            "package.json", "pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "go.mod",
            "go.work", "composer.json", "Gemfile", "Cargo.toml", "pom.xml", "build.gradle",
            "build.gradle.kts", "pubspec.yaml", "mix.exs", "pnpm-workspace.yaml", "turbo.json",
            "nx.json", "lerna.json"} or f.endswith(".csproj")]
        for m in manifests:
            name = Path(m).name
            deps_text += read(self.root / m) + "\n"
            stack.add({"package.json": "node", "pyproject.toml": "python", "requirements.txt": "python",
                       "Pipfile": "python", "setup.py": "python", "go.mod": "go", "go.work": "go",
                       "composer.json": "php", "Gemfile": "ruby", "Cargo.toml": "rust", "pom.xml": "java",
                       "build.gradle": "jvm", "build.gradle.kts": "jvm", "pubspec.yaml": "dart",
                       "mix.exs": "elixir"}.get(name, "monorepo" if name in {
                           "pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json", "go.work"} else "dotnet"))
        checks = {
            "nestjs": r"@nestjs/core", "nest-swagger": r"@nestjs/swagger", "express": r"\"express\"",
            "fastify": r"\"fastify\"", "nextjs": r"\"next\"", "react": r"\"react\"", "vue": r"\"vue\"",
            "angular": r"@angular/core", "prisma": r"\"prisma\"|@prisma/client", "django": r"(?i)\bdjango\b",
            "fastapi": r"(?i)\bfastapi\b", "flask": r"(?i)\bflask\b", "laravel": r"laravel/framework",
            "symfony": r"symfony/framework-bundle", "rails": r"['\"]rails['\"]", "spring": r"spring-boot",
            "springdoc": r"springdoc", "gin": r"gin-gonic/gin", "echo": r"labstack/echo", "fiber": r"gofiber",
            "flutter": r"sdk:\s*flutter", "phoenix": r":phoenix",
        }
        for k, rx in checks.items():
            if re.search(rx, deps_text):
                frameworks.add(k)
        if re.search(r"\"workspaces\"", deps_text):
            stack.add("monorepo")
        self.info["stack"] = sorted(stack)
        self.info["frameworks"] = sorted(frameworks)
        self.frameworks = frameworks

    UNIT_ROOTS = [
        "src/modules", "src/app/modules", "modules", "apps", "packages", "services", "libs",
        "internal", "cmd", "pkg", "src/features", "src/domains", "src/domain", "src/services",
        "app/Domains", "app/Modules", "src/main/java", "lib",
    ]

    def discover_units(self):
        units: dict[str, list[str]] = {}

        def code_child_dirs(base):
            out = []
            prefix = base + "/"
            children = {d[len(prefix):].split("/")[0] for d in self.repo.dirs if d.startswith(prefix)}
            for c in sorted(children):
                p = prefix + c
                if any(f.startswith(p + "/") and Path(f).suffix in CODE_EXT for f in self.repo.files):
                    out.append(c)
            return out

        for base in self.UNIT_ROOTS:
            if base in self.repo.dirs:
                kids = code_child_dirs(base)
                if base == "src/main/java":
                    continue  # profondeur de package Java : traité via le repli
                if len(kids) >= 2:
                    units[base] = kids
        # Django : apps contenant apps.py
        dj = defaultdict(list)
        for f in self.repo.files:
            if f.endswith("/apps.py"):
                parent = f.rsplit("/", 1)[0]
                base, _, name = parent.rpartition("/")
                dj[base or "."].append(name)
        for b, names in dj.items():
            if len(names) >= 2:
                units[b] = sorted(names)
        # repli : sous-dossiers de code du dossier source principal
        if not units:
            for base in ("src", "app", "lib", Path(self.root).name):
                if base in self.repo.dirs:
                    kids = code_child_dirs(base)
                    if len(kids) >= 3:
                        units[base] = kids
                        break
        self.units = units
        self.info["units"] = {b: len(v) for b, v in units.items()}

    # ---- doc <-> code : carte des unités -----------------------------------
    def check_unit_map(self):
        living = {k: v for k, v in self.docs.items() if self.doc_kind[k] == "vivante"}
        for base, names in self.units.items():
            parent = base.split("/")[-1] if base != "." else ""
            if not names:
                continue
            pat_for = lambda n: re.compile(
                rf"(?<![\w-])({re.escape(parent)}/)?{re.escape(n)}/" if parent else rf"(?<![\w-]){re.escape(n)}/")
            strict = {n: pat_for(n) for n in names}
            # doc « carte » : celle qui référence le plus d'unités par chemin
            best, best_cov = None, 0.0
            for d, t in living.items():
                cov = sum(1 for n in names if strict[n].search(t)) / len(names)
                if cov > best_cov:
                    best, best_cov = d, cov
            mentioned_anywhere = set()
            for n in names:
                loose = re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])")
                if any(loose.search(t) for t in living.values()):
                    mentioned_anywhere.add(n)
            never = [n for n in names if n not in mentioned_anywhere]
            if best and best_cov >= 0.4:
                t = living[best]
                missing = [n for n in names if not strict[n].search(t)]
                if missing:
                    sev = "important"
                    self.add(sev, "carte-modules", f"Unités de `{base}/` absentes de la carte {best}",
                             f"{len(missing)}/{len(names)} absentes : {', '.join(missing[:25])}"
                             + (" …" if len(missing) > 25 else ""), [best],
                             "Ajouter chaque unité avec ses routes / sa responsabilité réelles (lire le code).")
                # entrées de la carte qui n'existent plus
                if parent:
                    ref = re.compile(rf"(?<![\w-]){re.escape(parent)}/([A-Za-z0-9_.-]+)/")
                    cited = {m.group(1) for m in ref.finditer(t)}
                    gone = sorted(c for c in cited if c not in names and f"{base}/{c}" not in self.repo.dirs
                                  and not any(f"{b2}/{c}" in self.repo.dirs for b2 in self.units))
                    self.reported_gone |= {f"{parent}/{g}" for g in gone}
                    if gone:
                        self.add("important", "carte-modules", f"La carte {best} cite des unités disparues",
                                 f"Cités mais absents de `{base}/` : {', '.join(gone)}", [best],
                                 "Retrouver la suppression : git log --diff-filter=D --format='%cd %s' -1 -- '<chemin>/*'")
                # compteurs
                for m in re.finditer(r"[~≈]?\s?(\d{2,4})\s+(modules?|packages?|services|apps|unités)", t, re.I):
                    claimed = int(m.group(1))
                    if abs(claimed - len(names)) >= max(2, len(names) * 0.05):
                        self.add("mineur", "carte-modules", f"Compteur périmé dans {best}",
                                 f"La doc annonce « {m.group(0).strip()} », le code en compte {len(names)} dans `{base}/`.",
                                 [best])
                        break
            elif never and self._hub_map_for(names):
                self.info.setdefault("unit_map", {})[base] = {"doc": self._hub_map_for(names), "coverage": None}
                continue  # la carte des unités vit dans le hub : contrôlée par la vue « hub »
            elif never:
                self.add("important" if len(never) >= 3 else "mineur", "carte-modules",
                         f"Unités de `{base}/` jamais mentionnées dans la doc",
                         f"{len(never)}/{len(names)} jamais citées : {', '.join(never[:25])}"
                         + ("" if best else " (aucune carte des unités trouvée)"))
            self.info.setdefault("unit_map", {})[base] = {"doc": best, "coverage": round(best_cov, 2)}

    # ---- doc <-> code : chemins, liens, chemins absolus --------------------
    PATH_TOKEN = re.compile(r"`([^`\s]{2,200})`")
    MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    ABS_PATH = re.compile(r"(?<![\w/])([A-Za-z]:[\\/][^\s`'\")\]|<>]+|/(?:Users|home)/[^\s`'\")\]|<>]+)")

    def _looks_like_path(self, tok: str) -> bool:
        if any(c in tok for c in "<>*{}$|()[]=,;\"'") or tok.startswith(("http", "@", "-", "/", "~", "#", "--")):
            return False
        if "::" in tok or tok.count(":") > 0 or "..." in tok or "…" in tok:
            return False
        if re.match(r"^\d", tok):
            return False
        has_slash = "/" in tok.strip("/") or tok.endswith("/")
        ext = Path(tok.rstrip("/")).suffix.lower()
        known_ext = ext in CODE_EXT | DOC_EXT | {".json", ".yml", ".yaml", ".toml", ".sql", ".prisma",
                                                  ".sh", ".env", ".lock", ".html", ".css", ".scss"}
        if not (has_slash or known_ext):
            return False
        if re.match(r"^[a-z]+/[a-z+.-]+$", tok) and not known_ext and tok.split("/")[0] in {
                "application", "text", "image", "multipart", "audio", "video"}:
            return False  # types MIME
        first = tok.lstrip("./").split("/")[0]
        if first in self.repo.top_level:
            return True
        if not known_ext and not tok.endswith("/"):
            return False  # probablement une route d'API (`admin/refunds`) ou un libellé
        if "/" not in tok.strip("/") and not known_ext:
            return False  # `integrations/` seul : ambigu (autre dépôt, hub externe)
        if "/" in tok.strip("/") and first not in self.repo.dir_names:
            return False  # référence vers un autre dépôt / dossier externe : non vérifiable
        return True

    def _hub_map_for(self, names):
        """Doc d'un hub qui liste au moins 40 % de ces unités (listes de noms entre backticks)."""
        if not self.eco:
            return None
        names = set(names)
        for h in self._other_hubs():
            hub = self.eco.light(self.eco.paths[h])
            for d, t in hub.docs.items():
                listed = set()
                for line in t.splitlines():
                    toks = re.findall(r"`([^`]+)`", line)
                    if len(toks) >= 3:
                        listed |= set(toks)
                if names and len(listed & names) / len(names) >= 0.4:
                    return f"{self.eco.label(h)}/{d}"
        return None

    def _other_hubs(self):
        if not self.eco:
            return []
        me = self.root.resolve()
        return [h for h in self.eco.hubs if self.eco.paths[h] != me]

    def check_paths_and_links(self):
        stale_living, stale_hist = defaultdict(list), defaultdict(list)
        broken_links = defaultdict(list)
        external = defaultdict(set)
        eco = self.eco
        me = self.root.resolve()
        xbroken = defaultdict(lambda: defaultdict(set))   # dépôt cible -> doc -> références
        xok = defaultdict(int)                             # dépôt cible -> références résolues
        abs_related = defaultdict(set)                     # dépôt lié cité par chemin absolu -> docs
        hubs = self._other_hubs()
        deferred = defaultdict(set)  # hub : chemins des fiches qui décrivent un dépôt lié (contrôlés par _hub_view)
        if eco and eco.is_hub(me):
            for p in eco.unique_paths():
                if p == me:
                    continue
                for d, chunk in self._target_chunks(self.docs, eco.aliases(p)):
                    deferred[d] |= set(self.PATH_TOKEN.findall(chunk))
        for d, t in self.docs.items():
            ddir = d.rsplit("/", 1)[0] if "/" in d else ""
            kind = self.doc_kind[d]
            for m in self.ABS_PATH.finditer(t):
                p = re.sub(r"/+", "/", m.group(1).replace("\\", "/")).rstrip(".,:;")
                res = eco.resolve_abs(p) if eco else None
                if res:
                    key, rest = res
                    label = eco.label(key)
                    if eco.paths[key] != me:
                        abs_related[label].add(d)
                    if rest and not eco.repo(key).exists_loose(rest, ""):
                        xbroken[label][d].add(p)
                    else:
                        xok[label] += 1
                    continue
                segs = p.split("/")
                external[("/".join(segs[:3]))].add(d)
            for m in self.PATH_TOKEN.finditer(t):
                tok = m.group(1)
                clean = tok.lstrip("./")
                first = clean.split("/")[0]
                # `ride-platform-docs/integrations/x.md` : préfixé par le nom d'un dépôt lié
                if eco and "/" in clean.strip("/") and first.lower() in eco.paths and eco.paths[first.lower()] != me \
                        and not any(c in tok for c in "<>*{}$ "):
                    key = first.lower()
                    rest = clean.split("/", 1)[1]
                    if rest and not eco.repo(key).exists_loose(rest, ""):
                        xbroken[eco.label(key)][d].add(tok)
                    else:
                        xok[eco.label(key)] += 1
                    continue
                if not self._looks_like_path(tok) and not (hubs and "/" in clean.strip("/")):
                    continue
                norm = tok.strip("/")
                if any(norm == g or norm.endswith("/" + g) for g in self.reported_gone):
                    continue  # déjà signalé dans la carte des unités
                if tok in deferred.get(d, ()):
                    continue
                if self.repo.exists_loose(tok, ddir):
                    continue
                # `integrations/payments.md` cité sans préfixe : chemin relatif au hub ?
                handled = False
                if any(c in tok for c in "<>*{}$|()[]=,;\"' ") or ":" in tok:
                    continue
                for h in hubs:
                    hr = eco.repo(h)
                    if first in hr.top_level and (has_known_ext(tok) or tok.endswith("/") or clean.count("/") >= 1):
                        handled = True
                        if hr.exists_loose(tok, ""):
                            xok[eco.label(h)] += 1
                        else:
                            xbroken[eco.label(h)][d].add(tok)
                        break
                if handled or not self._looks_like_path(tok):
                    continue
                (stale_living if kind == "vivante" else stale_hist)[d].append(tok)
            for m in self.MD_LINK.finditer(t):
                target = m.group(1)
                if re.match(r"^(https?:|mailto:|tel:|#|data:)", target) or self.ABS_PATH.match(target):
                    continue
                path = target.split("#")[0].split("?")[0]
                if not path:
                    continue
                path = re.sub(r"%20", " ", path)
                resolved = os.path.normpath(os.path.join(ddir, path)).replace("\\", "/")
                if resolved.startswith(".."):
                    # lien vers un dossier frère : vérifiable si c'est un dépôt lié
                    if eco:
                        absp = (self.root / ddir / path).resolve()
                        for rp in eco.unique_paths():
                            if rp != me and (absp == rp or rp in absp.parents):
                                label = eco.primary[rp]
                                if absp.exists():
                                    xok[label] += 1
                                else:
                                    xbroken[label][d].add(target)
                                break
                    continue
                if resolved not in self.repo.all_paths and resolved.strip("/") not in self.repo.all_paths:
                    broken_links[d].append(target)
        for label, by_doc in xbroken.items():
            living = {d: v for d, v in by_doc.items() if self.doc_kind.get(d) == "vivante"}
            other = {d: v for d, v in by_doc.items() if d not in living}
            for group, sev in ((living, "important"), (other, "mineur")):
                if not group:
                    continue
                n = sum(len(v) for v in group.values())
                sample = "; ".join(f"{d} → " + ", ".join(f"`{x}`" for x in sorted(v)[:4]) for d, v in sorted(group.items())[:8])
                self.add(sev, "inter-depots",
                         f"Références vers {label} introuvables" + ("" if sev == "important" else " (docs historiques)"),
                         f"{n} référence(s) dans {len(group)} doc(s) : {sample}", sorted(group)[:10],
                         f"Le fichier a été déplacé ou supprimé dans {label} : retrouver la nouvelle cible "
                         f"(git log --follow dans {label}) ou retirer la référence.")
        self.info["cross_refs"] = {"resolues": dict(xok), "cassees": {k: sum(len(v) for v in d.values()) for k, d in xbroken.items()}}
        if abs_related:
            all_docs = sorted({d for v in abs_related.values() for d in v})
            self.add("mineur", "inter-depots", "Chemins absolus vers des dépôts liés",
                     f"Résolus sur ce poste, mais propres à une machine ({', '.join(sorted(abs_related))}) "
                     f"dans {len(all_docs)} fichier(s).", all_docs[:10],
                     "Préférer `<nom-du-dépôt>/chemin` : le nom reste valable sur tous les postes.")
        for d, toks in stale_living.items():
            uniq = sorted(set(toks))
            self.add("important", "chemins-perimes", f"Chemins cités introuvables dans {d}",
                     ", ".join(f"`{x}`" for x in uniq[:15]) + (" …" if len(uniq) > 15 else ""), [d],
                     "Vérifier chaque chemin : renommé, supprimé, ou faux positif (exemple, placeholder).")
        if stale_hist:
            total = sum(len(set(v)) for v in stale_hist.values())
            self.add("mineur", "chemins-perimes", "Chemins introuvables dans des docs historiques ou génériques",
                     f"{total} chemins dans {len(stale_hist)} docs historiques (plans, specs, ADR…) ou de skills. "
                     "Normal si la doc est marquée comme historique ou décrit d'autres projets.", sorted(stale_hist)[:10])
        for d, links in broken_links.items():
            sev = "important" if self.doc_kind[d] == "vivante" else "mineur"
            self.add(sev, "liens-casses", f"Liens relatifs cassés dans {d}",
                     ", ".join(sorted(set(links))[:15]), [d])
        if external:
            ranked = sorted(external.items(), key=lambda kv: -len(kv[1]))
            all_docs = sorted({d for v in external.values() for d in v})
            self.add("important", "dependance-externe", "La doc dépend de chemins absolus hors dépôt",
                     f"{len(external)} emplacement(s) dans {len(all_docs)} fichier(s), introuvables sur un autre "
                     "poste ou en CI : " + ", ".join(f"{p} ({len(d)})" for p, d in ranked[:10]),
                     all_docs[:10],
                     "S'il s'agit d'un dépôt du projet, le déclarer (--related nom=chemin ou .doc-audit.json) "
                     "pour vérifier ces références ; sinon rapatrier le contenu ou rendre la lecture optionnelle.")

    # ---- doc <-> code : commandes citées ------------------------------------
    def check_commands(self):
        scripts, make_targets, just_recipes = set(), set(), set()
        pkg_found = make_found = just_found = False
        for f in self.repo.files:
            n = Path(f).name
            if n == "package.json":
                pkg_found = True
                try:
                    scripts |= set((json.loads(read(self.root / f)) or {}).get("scripts", {}).keys())
                except Exception:
                    pass
            elif n in {"Makefile", "makefile", "GNUmakefile"} or n.endswith(".mk"):
                make_found = True
                make_targets |= set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", read(self.root / f), re.M))
            elif n.lower() == "justfile":
                just_found = True
                just_recipes |= set(re.findall(r"^@?([A-Za-z0-9_-]+)[^\n:=]*:(?!=)", read(self.root / f), re.M))
        yarn_builtin = {"install", "add", "remove", "upgrade", "init", "dlx", "why", "info", "set",
                        "config", "workspace", "workspaces", "global", "link", "cache", "create", "exec",
                        "run", "up", "dedupe", "npm", "plugin", "version", "publish", "pack", "audit",
                        "outdated", "list", "test", "start", "build"}
        missing = defaultdict(set)
        readme = self.info["entry_points"].get("readme")
        for d, t in self.docs.items():
            if self.doc_kind[d] != "vivante":
                continue
            if pkg_found:
                for m in re.finditer(r"\b(?:npm|pnpm|bun|yarn)\s+run\s+([A-Za-z0-9:_.-]+)", t):
                    if m.group(1) not in scripts:
                        missing[d].add(m.group(0))
                for m in re.finditer(r"\b(?:yarn|pnpm)\s+([A-Za-z][A-Za-z0-9:_.-]*)", t):
                    s = m.group(1)
                    if s not in yarn_builtin and ":" in s and s not in scripts:
                        missing[d].add(m.group(0))
            if make_found:
                for m in re.finditer(r"(?:^|`|\$ |\n)\s*make\s+([A-Za-z0-9_.-]+)", t):
                    if m.group(1) not in make_targets:
                        missing[d].add(f"make {m.group(1)}")
            if just_found:
                for m in re.finditer(r"(?:^|`|\$ |\n)\s*just\s+([A-Za-z0-9_-]+)", t):
                    if m.group(1) not in just_recipes:
                        missing[d].add(f"just {m.group(1)}")
        for d, cmds in missing.items():
            sev = "bloquant" if d == readme else "important"
            self.add(sev, "commandes", f"Commandes citées inexistantes dans {d}",
                     ", ".join(f"`{c}`" for c in sorted(cmds)[:15]), [d],
                     "Comparer avec les scripts réels (package.json, Makefile, justfile).")

    # ---- doc <-> code : fichiers promis --------------------------------------
    def check_promised_files(self):
        verbs = (r"(ajout|ajoute|add|updat|mett|met à jour|mets à jour|voir|see|consult|lis|read|"
                 r"renseign|consign|tenir|maintain|maintien|référ|refer|follow|suivre)")
        def near(word):
            return rf"{verbs}[^\n]{{0,60}}\b{word}\b"
        promised = {
            "changelog": (near("changelog"), ("CHANGELOG", "CHANGES", "HISTORY")),
            "license": (near("licen[cs]e"), ("LICENSE", "LICENCE", "COPYING")),
            "contributing": (near("contributing"), ("CONTRIBUTING",)),
            "security": (near(r"security\.md"), ("SECURITY",)),
        }
        stems = {Path(f).stem.upper() for f in self.repo.files}
        for key, (rx, names) in promised.items():
            if any(n in stems for n in names):
                continue
            where = [d for d, t in self.docs.items() if self.doc_kind[d] == "vivante" and re.search(rx, t, re.I)
                     and not d.upper().startswith(names)]
            if where:
                self.add("important", "fichiers-promis", f"Fichier « {key} » évoqué par la doc mais absent",
                         f"Mentionné dans : {', '.join(where[:6])}", where[:6],
                         "Créer le fichier, ou corriger la doc qui l'exige.")

    # ---- doc <-> code : couverture OpenAPI -----------------------------------
    def check_api_docs(self):
        fw = self.frameworks
        offenders = []
        total_routes = total_doc = 0
        if "nestjs" in fw and "nest-swagger" in fw:
            for f in self.repo.files:
                if f.endswith(".controller.ts") and not f.endswith(".spec.ts"):
                    t = read(self.root / f)
                    routes = len(re.findall(r"@(Get|Post|Put|Patch|Delete|Sse|All|Options|Head)\(", t))
                    docd = len(re.findall(r"@ApiOperation\(", t))
                    total_routes += routes
                    total_doc += min(docd, routes)
                    if routes and docd < routes:
                        offenders.append((f, routes, docd))
                    if routes and "@ApiTags(" not in t:
                        self.info.setdefault("_no_tags", []).append(f)
            label = "@ApiOperation"
        elif "fastapi" in fw:
            rx = re.compile(r"@\w+\.(get|post|put|patch|delete)\(([^)]*)\)\s*\n\s*(?:async\s+)?def\s+\w+\([^)]*\)[^:]*:\s*\n(\s*[ru]?\"\"\")?", re.S)
            for f in self.repo.files:
                if f.endswith(".py"):
                    t = read(self.root / f)
                    routes = docd = 0
                    for m in rx.finditer(t):
                        routes += 1
                        if "summary=" in m.group(2) or "description=" in m.group(2) or m.group(3):
                            docd += 1
                    total_routes += routes
                    total_doc += docd
                    if routes and docd < routes:
                        offenders.append((f, routes, docd))
            label = "summary= / docstring"
        elif "spring" in fw and "springdoc" in fw:
            for f in self.repo.files:
                if f.endswith((".java", ".kt")):
                    t = read(self.root / f)
                    routes = len(re.findall(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b", t))
                    if "@RestController" not in t and "@Controller" not in t:
                        continue
                    routes -= len(re.findall(r"^@RequestMapping", t, re.M))
                    docd = len(re.findall(r"@Operation\(", t))
                    total_routes += max(routes, 0)
                    total_doc += min(docd, max(routes, 0))
                    if routes > 0 and docd < routes:
                        offenders.append((f, routes, docd))
            label = "@Operation"
        else:
            return
        self.info["api_doc_coverage"] = {"routes": total_routes, "documented": total_doc}
        if offenders:
            none = [o for o in offenders if o[2] == 0]
            partial = [o for o in offenders if o[2] > 0]
            if none:
                self.add("important", "api-doc", f"Contrôleurs sans aucune description OpenAPI ({label})",
                         "; ".join(f"{o[0]} ({o[1]} routes)" for o in none[:20]), [o[0] for o in none[:20]],
                         "Lire la convention du projet (préfixes, tags) et l'impact sur la segmentation "
                         "par audience AVANT d'annoter.")
            if partial:
                miss = sum(o[1] - o[2] for o in partial)
                self.add("mineur", "api-doc", f"Contrôleurs partiellement décrits ({label})",
                         f"{len(partial)} fichiers, environ {miss} routes sans description. Ex. : "
                         + "; ".join(f"{o[0]} {o[2]}/{o[1]}" for o in partial[:8]),
                         [o[0] for o in partial[:20]])
        no_tags = self.info.pop("_no_tags", [])
        if no_tags:
            self.add("mineur", "api-doc", "Contrôleurs sans @ApiTags", ", ".join(no_tags[:15]), no_tags[:15])

    # ---- fraîcheur ----------------------------------------------------------
    def check_freshness(self):
        dates = {}
        for d, t in self.docs.items():
            if not t.startswith("---"):
                continue
            fm = t.split("\n---", 1)[0]
            m = re.search(r"^(last[-_ ]?verified|last[-_ ]?reviewed|updated|last[-_ ]?updated|reviewed)\s*:\s*['\"]?(\d{4}-\d{2}-\d{2})",
                          fm, re.I | re.M)
            if m:
                dates[d] = m.group(2)
        today = dt.date.today()
        if dates:
            c = Counter(dates.values())
            top, n = c.most_common(1)[0]
            if n >= 5 and n / len(dates) >= 0.4:
                since = self._commits_since(top, code_only=True)
                self.add("mineur", "fraicheur", "Dates de vérification posées en masse",
                         f"{n}/{len(dates)} fichiers portent la même date {top}"
                         + (f" ; {since} commits sur le code depuis" if since is not None else "")
                         + ". Probable injection groupée plutôt que relecture.",
                         [d for d, v in dates.items() if v == top][:10])
            for d, v in dates.items():
                try:
                    age = (today - dt.date.fromisoformat(v)).days
                except ValueError:
                    continue
                if age > 90:
                    self.info.setdefault("_stale_fm", []).append(f"{d} ({age} j)")
            stale = self.info.pop("_stale_fm", [])
            if stale:
                self.add("mineur", "fraicheur", "Docs dont la date de vérification dépasse 90 jours",
                         ", ".join(stale[:15]), [s.split(" (")[0] for s in stale[:15]])
        if not self.repo.is_git:
            return
        old = []
        for d in self.docs:
            if self.doc_kind[d] != "vivante":
                continue
            last = self._last_change(d)
            if not last:
                continue
            try:
                age = (today - dt.date.fromisoformat(last)).days
            except ValueError:
                continue
            if age > 120:
                since = self._commits_since(last, code_only=True) or 0
                if since >= 50:
                    old.append((d, last, since))
        if old:
            old.sort(key=lambda x: -x[2])
            self.add("mineur", "fraicheur", "Docs vivantes non touchées depuis longtemps alors que le code a bougé",
                     "; ".join(f"{d} (dernier commit {l}, {s} commits code depuis)" for d, l, s in old[:10]),
                     [o[0] for o in old[:10]])

    def _commits_since(self, date, code_only=False):
        if not self.repo.is_git:
            return None
        commits, _ = self.repo.history()
        return sum(1 for dte, files in commits if dte >= date and
                   (any(Path(f).suffix in CODE_EXT for f in files) if code_only else files))

    def _last_change(self, path_rel):
        if not self.repo.is_git:
            return ""
        return self.repo.history()[1].get(path_rel, "")

    # ---- duplication --------------------------------------------------------
    def check_duplication(self):
        living = [d for d in self.docs if self.doc_kind[d] == "vivante"
                  and ("/" not in d or d.startswith(("docs/", ".skills/", ".github/")))]
        lines_by_doc = {}
        for d in living:
            s = set()
            in_code = False
            for line in self.docs[d].splitlines():
                if line.strip().startswith("```"):
                    in_code = not in_code
                    continue
                n = re.sub(r"\s+", " ", line.strip().lower().lstrip("-*>#0123456789. "))
                if len(n) >= 45 and not set(n) <= set("|-: ") and not n.startswith("|---"):
                    s.add(n)
            lines_by_doc[d] = s
        freq = Counter(l for s in lines_by_doc.values() for l in s)
        for d in lines_by_doc:
            lines_by_doc[d] = {l for l in lines_by_doc[d] if freq[l] <= 3}
        pairs = []
        docs = sorted(lines_by_doc)
        for i, a in enumerate(docs):
            for b in docs[i + 1:]:
                common = lines_by_doc[a] & lines_by_doc[b]
                if len(common) >= 4:
                    pairs.append((a, b, len(common), next(iter(common))))
        if pairs:
            pairs.sort(key=lambda x: -x[2])
            self.add("mineur", "duplication", "Mêmes consignes recopiées dans plusieurs docs",
                     "; ".join(f"{a} ↔ {b} : {n} lignes communes" for a, b, n, _ in pairs[:8])
                     + f". Exemple : « {pairs[0][3][:90]} »",
                     sorted({p[0] for p in pairs[:8]} | {p[1] for p in pairs[:8]}),
                     "Garder une seule source et faire pointer les autres fichiers vers elle.")

    # ---- inter-dépôts : ce que le hub affirme des dépôts ----------------------
    def check_hub_claims(self):
        """Hub audité : compare ses fiches à chaque dépôt lié. Dépôt audité : ce que les hubs disent de lui (hors score)."""
        eco = self.eco
        if not eco:
            return
        me = self.root.resolve()
        if eco.is_hub(me):
            for p in eco.unique_paths():
                if p != me and not eco.is_hub(p):
                    self._hub_view(me, p, scope="self")
        elif not eco.ecosystem_mode:  # en mode écosystème, l'audit du hub porte déjà ces constats
            for h in self._other_hubs():
                self._hub_view(eco.paths[h], me, scope="hub")

    def _hub_view(self, hub_path: Path, tgt_path: Path, scope: str):
        eco = self.eco
        hub = self if hub_path == self.root.resolve() else eco.light(hub_path)
        tgt = self if tgt_path == self.root.resolve() else eco.light(tgt_path)
        hlabel, tlabel = eco.primary[hub_path], eco.primary[tgt_path]
        names = {n for n in eco.aliases(tgt_path) if len(n) >= 3}
        units = {n for v in tgt.units.values() for n in v}
        skills = [f for f in tgt.repo.files if re.match(r"^(\.skills|\.claude/skills|\.cursor/rules)/", f)
                  and Path(f).suffix.lower() in DOC_EXT | {".mdc"}]
        skills_core = [f for f in skills if Path(f).stem.lower() not in {"readme", "template", "index"}]
        living = {d: t for d, t in hub.docs.items() if hub.doc_kind.get(d) == "vivante"}
        for d, text in self._target_chunks(living, names):
            where = f"{hlabel}/{d}"
            # 1. carte des unités par noms (`auth`, `admin-auth`, …)
            if units:
                listed = set()
                for line in text.splitlines():
                    toks = re.findall(r"`([^`]+)`", line)
                    if len(toks) >= 3:
                        listed |= {x for x in toks if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", x)}
                cov = len(listed & units) / len(units)
                if cov >= 0.4:
                    missing = sorted(units - listed)
                    gone = sorted(x for x in listed - units
                                  if x not in tgt.repo.dir_names and not any(Path(f).stem == x for f in tgt.repo.files[:20000]))
                    if missing:
                        self.add("important", "inter-depots", f"La fiche {where} omet des unités de {tlabel}",
                                 f"{len(missing)}/{len(units)} absentes : {', '.join(missing[:25])}", [d],
                                 f"Mettre la carte du hub à jour depuis {tlabel} (ou la remplacer par un renvoi vers la carte locale).",
                                 scope)
                    if gone:
                        self.add("important", "inter-depots", f"La fiche {where} cite des unités disparues de {tlabel}",
                                 ", ".join(gone), [d], None, scope)
            # 2. compteurs (« ~58 modules », « (15 fichiers) »)
            for m in re.finditer(r"[~≈]?\s?\b(\d{1,4})\s+(modules?|fichiers|files|skills)\b", text, re.I):
                claim, kind = int(m.group(1)), m.group(2).lower()
                actual = len(units) if kind.startswith("module") else len(skills_core) if skills_core else None
                alt = len(skills) if not kind.startswith("module") else actual
                if not actual or claim in {actual, alt} or min(abs(claim - actual), abs(claim - (alt or actual))) <= 1:
                    continue
                sev = "important" if abs(claim - actual) / max(actual, 1) > 0.25 else "mineur"
                what = "unités" if kind.startswith("module") else "fichiers d'instructions (.skills…)"
                self.add(sev, "inter-depots", f"Compteur périmé dans {where}",
                         f"« {m.group(0).strip()} » pour {tlabel}, qui en compte {actual} ({what}).", [d], None, scope)
            # 3. chemins de la cible cités par le hub (`src/modules/live-map/`)
            missing_paths = set()
            for m in self.PATH_TOKEN.finditer(text):
                tok = m.group(1)
                clean = tok.lstrip("./")
                if any(c in tok for c in "<>*{}$|()[]=,;\"' :") or tok.startswith(("http", "@", "/", "~")):
                    continue
                if "/" not in clean.strip("/") and not has_known_ext(tok):
                    continue
                if hub.repo.exists_loose(tok, ""):
                    continue
                first = clean.split("/")[0]
                if first.lower() in names:
                    clean = clean.split("/", 1)[1] if "/" in clean else ""
                    first = clean.split("/")[0]
                if not clean or not (first in tgt.repo.top_level or has_known_ext(tok)):
                    continue
                if not tgt.repo.exists_loose(clean, ""):
                    missing_paths.add(tok)
            if missing_paths:
                self.add("important", "inter-depots", f"La fiche {where} cite des chemins absents de {tlabel}",
                         ", ".join(f"`{x}`" for x in sorted(missing_paths)[:15]), [d], None, scope)
            # 4. fraîcheur de la fiche vs activité du dépôt décrit
            if hub.repo.is_git and tgt.repo.is_git:
                last = hub._last_change(d)
                if last:
                    n = tgt._commits_since(last, code_only=True) or 0
                    if n >= 30:
                        self.add("mineur", "inter-depots", f"Fiche {where} figée pendant que {tlabel} évolue",
                                 f"Dernier commit de la fiche : {last} ; {n} commits sur le code de {tlabel} depuis.",
                                 [d], None, scope)

    @staticmethod
    def _target_chunks(docs, names):
        """(doc, texte) des fiches qui décrivent un dépôt : nom dans le nom du fichier, ou sections titrées par ce nom."""
        names = {n for n in names if len(n) >= 3}
        rx = [re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])", re.I) for n in names]
        out = []
        for d, t in docs.items():
            stem = Path(d).stem.lower()
            if any(n in stem for n in names):
                out.append((d, t))
                continue
            secs = [s for h, s in split_sections(t) if h and any(r.search(h) for r in rx)]
            if secs:
                out.append((d, "\n".join(secs)))
        return out

    def check_hub_duplication(self):
        """Contenu du hub recopié dans ce dépôt (contraire à la règle « source unique »)."""
        if not self.eco:
            return
        hubs = self._other_hubs()
        if not hubs:
            return
        def lines(text):
            s, code = set(), False
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    code = not code
                    continue
                n = re.sub(r"\s+", " ", line.strip().lower().lstrip("-*>#0123456789. "))
                if len(n) >= 45 and not set(n) <= set("|-: "):
                    s.add(n)
            return s
        mine = {d: lines(t) for d, t in self.docs.items() if self.doc_kind[d] == "vivante"}
        pairs = []
        for h in hubs:
            hub = self.eco.light(self.eco.paths[h])
            for hd, ht in hub.docs.items():
                hl = lines(ht)
                for d, ml in mine.items():
                    common = ml & hl
                    if len(common) >= 5:
                        pairs.append((d, f"{self.eco.label(h)}/{hd}", len(common), next(iter(common))))
        if pairs:
            pairs.sort(key=lambda x: -x[2])
            self.add("mineur", "inter-depots", "Contenu du hub recopié dans ce dépôt",
                     "; ".join(f"{a} ↔ {b} : {n} lignes" for a, b, n, _ in pairs[:8])
                     + f". Exemple : « {pairs[0][3][:90]} »", sorted({p[0] for p in pairs[:8]}),
                     "Garder le contenu dans le hub et le remplacer ici par un renvoi.")

    # ---- secrets ------------------------------------------------------------
    def check_secrets(self):
        if not self.repo.is_git:
            return
        tracked = run(["git", "ls-files"], self.root).splitlines()
        rx = re.compile(r"(^|/)(\.env(\.[\w-]+)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa|id_ed25519|"
                        r".*adminsdk.*\.json|credentials\.json|service[-_]?account.*\.json|.*\.keystore|.*\.jks)$", re.I)
        allowed = re.compile(r"\.env\.(example|sample|template|dist|test\.example)$|(^|/)\.env\.d/", re.I)
        bad = [f for f in tracked if rx.search(f) and not allowed.search(f)]
        if bad:
            self.add("bloquant", "secrets", "Fichiers sensibles suivis par git",
                     ", ".join(bad[:15]), bad[:15],
                     "Ne rien supprimer sans accord : signaler, proposer git rm --cached + rotation des clés.")

    # ---- CI ---------------------------------------------------------------
    def ci_info(self):
        wf = []
        for f in self.repo.files:
            if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml")) or f in {
                    ".gitlab-ci.yml", "bitbucket-pipelines.yml", ".circleci/config.yml", "azure-pipelines.yml"}:
                t = read(self.root / f)
                trig = []
                m = re.search(r"^[\"']?on[\"']?:[ \t]*(.*?)(?=^\S)", t + "\nEND", re.M | re.S)
                if m:
                    block = m.group(1)
                    first_line = block.split("\n", 1)[0].strip()
                    if first_line:
                        trig = re.findall(r"[a-z_]+", first_line)
                    else:
                        trig = re.findall(r"^ {2}([a-z_]+):?", block, re.M)
                doc_steps = re.findall(r"(?:run|script):\s*(.*(?:doc|markdown|mdlint|lychee|link|vale|openapi|swagger).*)",
                                       t, re.I)
                wf.append({"file": f, "triggers": sorted(set(trig)), "doc_steps": [s.strip() for s in doc_steps][:5]})
        self.info["ci"] = wf

    # ---- score --------------------------------------------------------------
    def score(self):
        pts = defaultdict(int)
        for f in self.findings:
            if f.get("scope", "self") == "self":
                pts[f["severity"]] += WEIGHTS[f["severity"]]
        loss = sum(min(pts[s], CAPS[s]) for s in pts)
        sc = max(0, 100 - loss)
        grade = "A" if sc >= 90 else "B" if sc >= 75 else "C" if sc >= 60 else "D" if sc >= 40 else "E"
        return sc, grade

    def run_all(self):
        self.load_docs()
        self.inventory()
        self.detect_stack()
        self.discover_units()
        for step in (self.check_unit_map, self.check_paths_and_links, self.check_commands,
                     self.check_promised_files, self.check_api_docs, self.check_freshness,
                     self.check_duplication, self.check_hub_claims, self.check_hub_duplication,
                     self.check_secrets, self.ci_info):
            try:
                step()
            except Exception as e:  # un contrôle cassé ne doit pas bloquer les autres
                self.info.setdefault("errors", []).append(f"{step.__name__}: {e}")
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"]))
        if self.eco:
            me = self.root.resolve()
            self.info["ecosystem"] = {
                "depots_lies": sorted({self.eco.primary[p] for p in self.eco.unique_paths() if p != me}),
                "hubs": [self.eco.label(h) for h in self.eco.hubs],
                "ce_depot_est_un_hub": self.eco.is_hub(me),
                "config": self.eco.config_file,
            }


# --------------------------------------------------------------------------- sortie


def _findings_md(items, max_items, level="##"):
    out = []
    for sev in ("bloquant", "important", "mineur"):
        sel = [f for f in items if f["severity"] == sev]
        if not sel:
            continue
        out.append(f"{level} {sev.capitalize()} ({len(sel)})")
        for f in sel[:max_items]:
            out.append(f"- **[{f['category']}] {f['title']}** — {f['evidence']}")
            if f.get("hint"):
                out.append(f"  - Piste : {f['hint']}")
        if len(sel) > max_items:
            out.append(f"- … {len(sel) - max_items} autres (utiliser --json)")
        out.append("")
    return out


def to_markdown(a: Audit, max_items: int, level: int = 1) -> str:
    sc, grade = a.score()
    i = a.info
    h1, h2 = "#" * level, "#" * (level + 1)
    own = [f for f in a.findings if f.get("scope", "self") == "self"]
    hub_view = [f for f in a.findings if f.get("scope") == "hub"]
    c = Counter(f["severity"] for f in own)
    title = a.eco.primary.get(a.root.resolve(), a.root.name) if a.eco else a.root.name
    out = [f"{h1} Audit doc ↔ code — {title}", ""]
    out.append(f"**Score heuristique : {sc}/100 ({grade})** — "
               f"{c.get('bloquant', 0)} bloquant(s), {c.get('important', 0)} important(s), {c.get('mineur', 0)} mineur(s).")
    out.append("")
    out.append("> Constats heuristiques : chacun doit être vérifié avant d'être rapporté (faux positifs possibles).")
    out.append("")
    out.append(f"{h2} Inventaire")
    out.append(f"- Stack : {', '.join(i.get('stack', [])) or '?'} — frameworks : {', '.join(i.get('frameworks', [])) or '—'}")
    ep = i.get("entry_points", {})
    out.append("- Points d'entrée : " + ", ".join(f"{k}={'✓ ' + v if v else '✗'}" for k, v in ep.items()))
    out.append(f"- Docs : {i.get('doc_files', 0)} fichiers ({i.get('doc_files_living', 0)} vivants, "
               f"{i.get('doc_files_historic', 0)} historiques) ; instructions d'agents : {i.get('agent_instruction_files', 0)} ; "
               f"specs/plans : {i.get('specs_plans', 0)} ; ADR : {', '.join(i.get('adr_dirs', [])) or '—'}")
    for base, n in i.get("units", {}).items():
        um = i.get("unit_map", {}).get(base, {})
        out.append(f"- Unités `{base}/` : {n} — carte : {um.get('doc') or 'aucune'}"
                   + (f" (couverture {int(um['coverage'] * 100)} %)" if um.get("doc") and um.get("coverage") is not None
                      else " (dans le hub)" if um.get("doc") else ""))
    if "api_doc_coverage" in i:
        r = i["api_doc_coverage"]
        pct = int(100 * r["documented"] / r["routes"]) if r["routes"] else 100
        out.append(f"- Couverture OpenAPI : {r['documented']}/{r['routes']} routes décrites ({pct} %)")
    for w in i.get("ci", []):
        out.append(f"- CI `{w['file']}` — déclencheurs : {', '.join(w['triggers']) or '?'}"
                   + (f" — étapes doc : {' | '.join(w['doc_steps'])}" if w["doc_steps"] else ""))
    eco = i.get("ecosystem")
    if eco:
        xr = i.get("cross_refs", {})
        ok = ", ".join(f"{k} {v}" for k, v in sorted(xr.get("resolues", {}).items())) or "—"
        ko = ", ".join(f"{k} {v}" for k, v in sorted(xr.get("cassees", {}).items())) or "—"
        out.append(f"- Dépôts liés : {', '.join(eco['depots_lies']) or 'aucun'} — hubs : {', '.join(eco['hubs']) or 'aucun'}"
                   + (" (ce dépôt est un hub)" if eco["ce_depot_est_un_hub"] else "")
                   + (f" — config : {eco['config']}" if eco.get("config") else ""))
        out.append(f"- Références inter-dépôts : résolues [{ok}] ; cassées [{ko}]")
    out.append("")
    out += _findings_md(own, max_items, h2)
    if hub_view:
        out.append(f"{h2} Ce que le hub dit de ce dépôt (hors score)")
        out.append("")
        out.append("Constats sur les fiches du hub qui décrivent ce dépôt : à corriger dans le hub.")
        out.append("")
        out += _findings_md(hub_view, max_items, "#" * (level + 2))
    if i.get("errors"):
        out.append(f"{h2} Contrôles en erreur")
        out += [f"- {e}" for e in i["errors"]]
        out.append("")
    if level == 1:
        out.append(f"{h2} Non couvert par le script (à juger par l'agent)")
        out.append("- Règles énoncées en prose (« chaque endpoint a… ») autres que la couverture OpenAPI.")
        out.append("- Exactitude du contenu (routes, rôles, flux décrits) et qualité pédagogique.")
        out.append("- Effets de bord d'une annotation (segmentation de doc par audience, tags).")
        out.append("- Contrats partagés entre dépôts (format de réponse, flux de paiement) décrits différemment.")
    return "\n".join(out) + "\n"


def ecosystem_markdown(audits, max_items):
    out = ["# Audit doc ↔ code — écosystème", ""]
    out.append("| Dépôt | Rôle | Score | Bloquant | Important | Mineur |")
    out.append("|-------|------|-------|----------|-----------|--------|")
    for a in audits:
        sc, g = a.score()
        c = Counter(f["severity"] for f in a.findings if f.get("scope", "self") == "self")
        role = "hub" if a.eco.is_hub(a.root.resolve()) else "dépôt"
        out.append(f"| {a.eco.primary[a.root.resolve()]} | {role} | {sc} ({g}) | {c.get('bloquant', 0)} | "
                   f"{c.get('important', 0)} | {c.get('mineur', 0)} |")
    out.append("")
    out.append("> Constats heuristiques : chacun doit être vérifié avant d'être rapporté. "
               "Les écarts entre le hub et un dépôt sont comptés dans le score du hub.")
    out.append("")
    for a in audits:
        out.append(to_markdown(a, max_items, level=2))
    out.append("## Non couvert par le script (à juger par l'agent)")
    out.append("- Contrats partagés entre dépôts (format de réponse, flux de paiement) décrits différemment.")
    out.append("- Exactitude du contenu et qualité pédagogique ; effets de bord des annotations.")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Audit heuristique doc <-> code (lecture seule).")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    ap.add_argument("--out", help="écrire le rapport dans ce fichier")
    ap.add_argument("--max", type=int, default=30, help="constats max par gravité (markdown)")
    ap.add_argument("--related", action="append", default=[], metavar="NOM=CHEMIN",
                    help="dépôt lié (répétable) ; s'ajoute à .doc-audit.json et aux dossiers frères détectés")
    ap.add_argument("--hub", action="append", default=[], metavar="NOM",
                    help="dépôt servant de hub de documentation (sinon détecté : dépôt quasi sans code)")
    ap.add_argument("--no-auto", action="store_true", help="ne pas détecter les dossiers frères")
    ap.add_argument("--single", action="store_true", help="ignorer tout dépôt lié (audit d'un seul dépôt)")
    ap.add_argument("--no-git", action="store_true", help="ne pas lire git (fraîcheur, secrets) : plus rapide")
    ap.add_argument("--ecosystem", action="store_true",
                    help="auditer le dépôt et tous ses dépôts liés, avec un score par dépôt")
    args = ap.parse_args()
    global NO_GIT
    NO_GIT = args.no_git
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"Dossier introuvable : {root}")
    related = {}
    for spec in args.related:
        if "=" not in spec:
            sys.exit(f"--related attend NOM=CHEMIN : {spec}")
        n, p = spec.split("=", 1)
        related[n.strip()] = Path(p.strip()).expanduser()
    eco = None if args.single else Ecosystem(root, related, args.hub, auto=not args.no_auto,
                                               ecosystem_mode=args.ecosystem)
    if eco and len(eco.unique_paths()) == 1:
        eco = None  # aucun dépôt lié trouvé : audit simple
    if args.ecosystem and eco:
        audits = []
        order = sorted(eco.unique_paths(), key=lambda p: (not eco.is_hub(p), p != eco.root, eco.primary[p]))
        for p in order:
            a = Audit(p, eco)
            a.run_all()
            audits.append(a)
        if args.json:
            text = json.dumps([{"repo": eco.primary[a.root.resolve()], "root": str(a.root), "score": a.score()[0],
                                "grade": a.score()[1], "info": a.info, "findings": a.findings} for a in audits],
                              ensure_ascii=False, indent=2)
        else:
            text = ecosystem_markdown(audits, args.max)
    else:
        a = Audit(root, eco)
        a.run_all()
        if args.json:
            sc, grade = a.score()
            text = json.dumps({"root": str(root), "score": sc, "grade": grade, "info": a.info,
                               "findings": a.findings}, ensure_ascii=False, indent=2)
        else:
            text = to_markdown(a, args.max)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Rapport écrit : {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        print(text)


if __name__ == "__main__":
    main()
