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
        self.is_git = (root / ".git").exists()
        self.top_level = {p.split("/")[0] for p in self.all_paths}
        self.dir_names = {d.split("/")[-1] for d in self.dirs}

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


# --------------------------------------------------------------------------- collecte


class Audit:
    def __init__(self, root: Path):
        self.root = root
        self.repo = Repo(root)
        self.findings: list[dict] = []
        self.info: dict = {}
        self.docs: dict[str, str] = {}
        self.doc_kind: dict[str, str] = {}  # vivante | historique
        self.reported_gone: set[str] = set()  # "<parent>/<unité>" déjà signalés par la carte

    def add(self, severity, category, title, evidence, files=None, hint=None):
        self.findings.append({
            "severity": severity, "category": category, "title": title,
            "evidence": evidence, "files": files or [], "hint": hint,
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

        if not entry["readme"]:
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
        if re.match(r"^[a-z]+/[a-z]+$", tok) and not known_ext and tok.split("/")[0] in {
                "application", "text", "image", "multipart", "audio", "video"}:
            return False  # types MIME
        return True

    def check_paths_and_links(self):
        stale_living, stale_hist = defaultdict(list), defaultdict(list)
        broken_links = defaultdict(list)
        external = defaultdict(set)
        for d, t in self.docs.items():
            ddir = d.rsplit("/", 1)[0] if "/" in d else ""
            kind = self.doc_kind[d]
            for m in self.ABS_PATH.finditer(t):
                p = re.sub(r"/+", "/", m.group(1).replace("\\", "/"))
                segs = p.split("/")
                external[("/".join(segs[:3]))].add(d)
            for m in self.PATH_TOKEN.finditer(t):
                tok = m.group(1)
                if not self._looks_like_path(tok):
                    continue
                norm = tok.strip("/")
                if any(norm == g or norm.endswith("/" + g) for g in self.reported_gone):
                    continue  # déjà signalé dans la carte des unités
                if not self.repo.exists_loose(tok, ddir):
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
                    continue  # hors dépôt : traité comme dépendance externe
                if resolved not in self.repo.all_paths and resolved.strip("/") not in self.repo.all_paths:
                    broken_links[d].append(target)
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
                     "Rapatrier dans le dépôt (sous-module, copie versionnée) ou rendre la lecture optionnelle.")

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
            last = run(["git", "log", "-1", "--format=%cs", "--", d], self.root).strip()
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
        out = run(["git", "log", f"--since={date}", "--format=%H", "--name-only"], self.root, timeout=120)
        if not out:
            return 0
        n, cur, touched = 0, None, False
        for line in out.splitlines() + [""]:
            if re.fullmatch(r"[0-9a-f]{40}", line):
                if cur and touched:
                    n += 1
                cur, touched = line, False
            elif line and (not code_only or Path(line).suffix in CODE_EXT):
                touched = True
        if cur and touched:
            n += 1
        return n

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
                     self.check_duplication, self.check_secrets, self.ci_info):
            try:
                step()
            except Exception as e:  # un contrôle cassé ne doit pas bloquer les autres
                self.info.setdefault("errors", []).append(f"{step.__name__}: {e}")
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"]))


# --------------------------------------------------------------------------- sortie


def to_markdown(a: Audit, max_items: int) -> str:
    sc, grade = a.score()
    i = a.info
    c = Counter(f["severity"] for f in a.findings)
    out = [f"# Audit doc ↔ code — {a.root.name}", ""]
    out.append(f"**Score heuristique : {sc}/100 ({grade})** — "
               f"{c.get('bloquant', 0)} bloquant(s), {c.get('important', 0)} important(s), {c.get('mineur', 0)} mineur(s).")
    out.append("")
    out.append("> Constats heuristiques : chacun doit être vérifié avant d'être rapporté (faux positifs possibles).")
    out.append("")
    out.append("## Inventaire")
    out.append(f"- Stack : {', '.join(i.get('stack', [])) or '?'} — frameworks : {', '.join(i.get('frameworks', [])) or '—'}")
    ep = i.get("entry_points", {})
    out.append("- Points d'entrée : " + ", ".join(f"{k}={'✓ ' + v if v else '✗'}" for k, v in ep.items()))
    out.append(f"- Docs : {i.get('doc_files', 0)} fichiers ({i.get('doc_files_living', 0)} vivants, "
               f"{i.get('doc_files_historic', 0)} historiques) ; instructions d'agents : {i.get('agent_instruction_files', 0)} ; "
               f"specs/plans : {i.get('specs_plans', 0)} ; ADR : {', '.join(i.get('adr_dirs', [])) or '—'}")
    for base, n in i.get("units", {}).items():
        um = i.get("unit_map", {}).get(base, {})
        out.append(f"- Unités `{base}/` : {n} — carte : {um.get('doc') or 'aucune'}"
                   + (f" (couverture {int(um.get('coverage', 0) * 100)} %)" if um.get("doc") else ""))
    if "api_doc_coverage" in i:
        r = i["api_doc_coverage"]
        pct = int(100 * r["documented"] / r["routes"]) if r["routes"] else 100
        out.append(f"- Couverture OpenAPI : {r['documented']}/{r['routes']} routes décrites ({pct} %)")
    for w in i.get("ci", []):
        out.append(f"- CI `{w['file']}` — déclencheurs : {', '.join(w['triggers']) or '?'}"
                   + (f" — étapes doc : {' | '.join(w['doc_steps'])}" if w["doc_steps"] else ""))
    out.append("")
    for sev in ("bloquant", "important", "mineur"):
        items = [f for f in a.findings if f["severity"] == sev]
        if not items:
            continue
        out.append(f"## {sev.capitalize()} ({len(items)})")
        for f in items[:max_items]:
            out.append(f"- **[{f['category']}] {f['title']}** — {f['evidence']}")
            if f.get("hint"):
                out.append(f"  - Piste : {f['hint']}")
        if len(items) > max_items:
            out.append(f"- … {len(items) - max_items} autres (utiliser --json)")
        out.append("")
    if i.get("errors"):
        out.append("## Contrôles en erreur")
        out += [f"- {e}" for e in i["errors"]]
        out.append("")
    out.append("## Non couvert par le script (à juger par l'agent)")
    out.append("- Règles énoncées en prose (« chaque endpoint a… ») autres que la couverture OpenAPI.")
    out.append("- Exactitude du contenu (routes, rôles, flux décrits) et qualité pédagogique.")
    out.append("- Effets de bord d'une annotation (segmentation de doc par audience, tags).")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Audit heuristique doc <-> code (lecture seule).")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    ap.add_argument("--out", help="écrire le rapport dans ce fichier")
    ap.add_argument("--max", type=int, default=30, help="constats max par gravité (markdown)")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"Dossier introuvable : {root}")
    a = Audit(root)
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
