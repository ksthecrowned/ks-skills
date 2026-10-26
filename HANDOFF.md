# Reprise — plugin `audit-documentation` (marketplace `ks-skills`)

Rapport de passation, au 26/10/2026. Il s'adresse à l'agent qui reprend le projet. L'utilisateur parle français : rapports et messages en français.

## 1. Objectif

Un skill Claude Code réutilisable sur n'importe quel projet, en deux phases :
1. **Évaluer** la documentation : rapport avec preuves, sans rien modifier.
2. **Corriger** les endroits où la doc contredit le code, uniquement sur demande.

Il s'appuie sur un script de détection heuristique (`doc_audit.py`) et sur un barème de gravité. Le chantier en cours ajoute un **mode multi-dépôts**, où la doc est répartie entre un hub et plusieurs dépôts du même projet.

## 2. État du dépôt `ksthecrowned/ks-skills` (public)

| Branche | Contenu | État |
|---------|---------|------|
| `main` | v1.0.0 : audit d'un seul dépôt | Publiée ; installation depuis GitHub testée |
| `wip-multi-depots` | v1.0.0 + mode multi-dépôts + optimisations + ce rapport + `tests/` | Fonctionne sur les fixtures, **pas encore validée sur de vrais dépôts** |

Installation utilisateur : `/plugin marketplace add ksthecrowned/ks-skills` puis `/plugin install audit-documentation@ks-skills`.

```
.claude-plugin/marketplace.json                 # marketplace "ks-skills"
plugins/audit-documentation/
  .claude-plugin/plugin.json                    # version 1.0.0 (à passer en 1.1.0)
  README.md
  skills/audit-documentation/
    SKILL.md                                    # déroulé ; ne décrit PAS encore le mode multi-dépôts
    scripts/doc_audit.py                        # ~1380 lignes, Python 3.8+, stdlib, lecture seule
    references/severity.md                      # barème + score
    references/stacks.md                        # repères par stack
tests/make_fixtures.sh, tests/run_fixtures.sh   # fixtures reproductibles + scores de référence
```

Validation : `claude plugin validate .` et `claude plugin validate ./plugins/audit-documentation` passent. Seul avertissement : pas d'icône (facultatif).

## 3. Architecture de `doc_audit.py`

- **`Repo`** : inventaire des fichiers (dossiers lourds ignorés), index des suffixes pour résoudre des chemins cités partiellement (`modules/x/`), `history()` = **un seul `git log` par dépôt** (13 derniers mois, 1500 commits au plus), mis en cache.
- **`Ecosystem`** : dépôts liés, venant de trois sources : `--related nom=chemin`, `.doc-audit.json` (`{"related": {"nom": "chemin"}, "hub": "nom"}`), ou dossiers frères avec `.git`, **retenus seulement s'ils sont cités** dans un sens ou dans l'autre (deux passes, pour qu'un hub fasse entrer les dépôts qu'il cite).
  - Chaque dépôt est connu par le nom de son remote (lu directement dans `.git/config`, sans appel git) **et** par le nom de son dossier.
  - Le hub est soit déclaré, soit deviné : au moins 5 docs et presque pas de code.
  - `resolve_abs` associe `D:/Projects/x/...` au dépôt lié `x` (premier segment qui nomme un dépôt).
- **`Audit`** : contrôles indépendants (une exception dans l'un n'arrête pas les autres), constats `{severity, category, title, evidence, files, hint, scope}`.
  - Un seul dépôt : carte des unités, chemins et liens périmés, commandes citées inexistantes, fichiers promis absents, couverture OpenAPI (NestJS, FastAPI, Spring), fraîcheur, duplication, secrets suivis par git, déclencheurs CI.
  - Inter-dépôts (`category = "inter-depots"`) :
    - les références vers un autre dépôt dans leurs trois formes (absolue, préfixée par le nom du dépôt, relative au hub), y compris les liens markdown `../autre-depot/...` ;
    - `check_hub_claims` et `_hub_view` comparent les fiches du hub au dépôt qu'elles décrivent (fichier dont le nom contient le nom du dépôt, ou section titrée par ce nom) : unités omises ou disparues, compteurs (« ~58 modules », « (15 fichiers) »), chemins absents, fiche figée pendant que le dépôt évolue ;
    - `check_hub_duplication` repère le contenu du hub recopié dans un dépôt.
  - **`scope="hub"`** : quand on audite un dépôt seul, les constats sur ce que le hub dit de lui sont affichés à part et **exclus du score**. En mode `--ecosystem`, ils sont comptés dans le score du hub, pas en double.
- Sorties : markdown (défaut), `--json`, `--ecosystem` (tableau des scores par dépôt + rapport de chaque dépôt).
- Options : `--single` (ignore les dépôts liés), `--no-auto`, `--hub NOM`, `--no-git`, `--max N`, `--out FICHIER`.

## 4. Tests

```bash
bash tests/make_fixtures.sh /tmp/fx && bash tests/run_fixtures.sh /tmp/fx
```

Scores de référence (commit `fcc24af`) : fastapi 64 C, django 99 A, go 75 B, mono 65 C, laravel 98 A. Écosystème : platform-docs 63 C, shop-api 93 A, shop-app 89 B, et `unrelated` ne doit pas apparaître. Le script lancé sur `ks-skills` lui-même donne 99 A.

**Validation réelle partielle** sur `ksthecrowned/ride-api-fork` (état du 08/10, mode un seul dépôt) : 55 D. Il retrouve tout ce qui avait été relevé à la main : 7 controllers sans `@ApiOperation`, 7 modules absents de `docs/ARCHITECTURE_FOLDERS.md`, 3 modules disparus, changelog promis absent. Il ajoute des commandes inexistantes (`bun run test:e2e:payments`…) et la dépendance au hub `D:/Projects/ride-platform-docs`.

## 5. Ce qui reste à faire

1. **Valider le mode multi-dépôts sur l'écosystème réel de l'utilisateur.** C'est le point bloquant.
   - L'exécution à travers les dossiers montés dans la VM (`mcp__remote-devices__device_bash`) dépasse le délai maximal de 180 s, parce que chaque lecture et chaque appel git y sont très lents. **Ne pas réessayer par ce chemin**, l'utilisateur a interrompu pour cette raison.
   - Lui faire lancer le script **directement sous Windows**, ou cloner les dépôts dans le conteneur s'il donne l'accès GitHub :
     ```bash
     cd D:\Projects
     curl -o doc_audit.py https://raw.githubusercontent.com/ksthecrowned/ks-skills/wip-multi-depots/plugins/audit-documentation/skills/audit-documentation/scripts/doc_audit.py
     python doc_audit.py ride-api --ecosystem --related ride-client=D:\ride-client
     ```
   - Ce que le script devrait trouver, d'après les écarts déjà constatés à la main :
     - `ride-platform-docs/reference/ride-api-module-map.md` cite `insurance-type` et `delivery-fee-tier`, supprimés en juillet-août 2026, et omet `admin-search`, `delivery-tariff`, `ride-transport-tariff`, `maintenance` ;
     - `reference/skills-by-repo.md` annonce « ride-api (15 fichiers) » alors qu'il y en a 28 ;
     - `projects/ride-api.md` annonce « ~58 modules » alors qu'il y en a 62 ;
     - le dernier commit du hub date du 10/08, et ride-api a beaucoup bougé depuis.
   - Trier les faux positifs et ajuster les heuristiques. Ajouter si possible une fixture qui reproduit chaque faux positif corrigé.
2. **Documenter le mode multi-dépôts.**
   - `SKILL.md` : quand et comment l'utiliser, déclaration des dépôts liés, lecture de la section « Ce que le hub dit de ce dépôt (hors score) », règle « corriger dans le hub, pas dans le dépôt », et le fait que les contrats partagés restent à vérifier par l'agent.
   - `README.md` du plugin et `references/severity.md` (catégorie `inter-depots`).
   - Passer la version à **1.1.0** dans `plugin.json` et `marketplace.json`.
3. **Fusionner** `wip-multi-depots` dans `main` en un seul commit, relancer les deux `claude plugin validate`, supprimer la branche. **Demander l'accord de l'utilisateur avant de fusionner et de pousser sur `main`.** Ne pas garder `HANDOFF.md` dans `main`, sauf si l'utilisateur le souhaite. Conserver `tests/`.
4. Facultatif :
   - icône du plugin (PNG carré de 512 à 2048 px, dans `.claude-plugin/icon.png`) ;
   - contrôle automatique des contrats partagés (par exemple une liste `contracts` dans `.doc-audit.json`) ;
   - paralléliser l'audit des dépôts en mode écosystème.

## 6. Limites connues et pièges

- **Heuristique** : chaque constat est une piste, à vérifier avant d'être rapporté (le SKILL.md l'impose). Faux positifs typiques :
  - exemple ou placeholder pris pour un chemin réel ;
  - doc historique classée « vivante » (le classement se fait par le chemin et l'en-tête du fichier, pas par le sommaire) ;
  - dossier vide pris pour un module.
- **Choix pour limiter les faux positifs.** Les cas suivants sont volontairement **non vérifiés** :
  - une route d'API citée (`admin/refunds`) ;
  - un chemin d'un seul segment (`integrations/`) ;
  - un chemin dont le premier segment n'existe nulle part.
- **Fichiers « meta »** (`SKILL.md`, `skills/*/references/`) : ils décrivent d'autres projets et sont exclus des contrôles stricts.
- **Fichiers promis** : un CHANGELOG ou une LICENSE n'est réclamé que si un verbe de consigne (ajouter, voir, mettre à jour…) apparaît à moins de 60 caractères.
- **Noms des dépôts** : le nom du remote peut différer de celui qu'utilise la doc. Dans l'écosystème réel, `ride-client` a pour remote `LrcGoup/ride-congo` et `dt-api` a pour remote `LrcGoup/dtmoney-api`. Le nom du dossier sert aussi d'alias : sous Windows, cela fonctionne tout seul. Dans la VM, les dossiers montés s'appellent par exemple `D:--ride-client`, d'où le besoin de `--related`.
- **Il existe deux hubs** : `ride-platform-docs`, et `dtmoney-platform-docs`, que dt-api cite mais auquel la session n'a pas accès. Ses références restent signalées comme dépendance externe tant qu'il n'est pas déclaré.

## 7. Contexte de l'environnement et de l'utilisateur

- Machine de l'utilisateur (Windows), dépôts dans `D:\Projects\` : ride-api, ride-platform-docs, dt-api, dtmoney-platform-docs, ride-admin-dashoard, ride-partner-dashboard, etc. Le vrai ride-client est dans `D:\ride-client` (`D:\Projects\ride-client` n'est qu'un raccourci `.lnk`).
- Accès accordés pendant cette session, en lecture : ride-api (lecture et écriture), ride-platform-docs, dt-api, `D:\ride-client`. Des copies du script et un rapport partiel traînent dans `$HOME/work` de la VM, hors des dossiers de l'utilisateur.
- Le jeton `gh` du conteneur est invalide. L'accès en push à `ksthecrowned/ks-skills` passe par l'outil d'ajout de dépôt de la session (`add_repo`, accès `push`). Avant de pousser depuis un clone partiel, faire `git fetch origin main`.
- Commits : auteur `ksthecrowned <ramaelbikoumou@gmail.com>`, avec les lignes d'attribution de la session.
- **Doublon à signaler** : une ancienne version du skill, enregistrée dans les skills du compte de l'utilisateur sous le même nom (`audit-documentation`), coexiste avec le plugin. L'utilisateur devrait la supprimer de son compte.
- **Travail annexe non commité** dans `D:\Projects\ride-api` (première demande de la session, corrections de doc) :
  - `@ApiOperation` ajouté sur 7 controllers ;
  - `docs/ARCHITECTURE_FOLDERS.md` remis en cohérence (62 modules) ;
  - `CHANGELOG.md` créé, avec des liens depuis README et CONTRIBUTING.
  
  Seule une vérification syntaxique a été faite. Il reste à lancer `bun run build`, `bun run docs:check` et `bun run test:e2e:swagger` avant de commiter. Ne pas commiter sans l'accord de l'utilisateur.
