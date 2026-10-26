---
name: audit-documentation
description: Évaluer la documentation d'un projet de code (toute stack) et corriger les endroits où elle contredit le code, avec un script de détection et un barème de gravité. Pour « que penses-tu de la doc », « audit doc », « la doc est-elle à jour ».
---

# Audit de documentation (doc ↔ code)

Deux phases séparées : **1. évaluer** (rapport, aucune modification), **2. corriger** les contradictions, seulement si l'utilisateur le demande. Répondre dans la langue de l'utilisateur.

Fichiers du skill :
- `scripts/doc_audit.py` — détection heuristique, Python 3.8+ sans dépendance, lecture seule.
- `references/severity.md` — barème de gravité et calcul du score.
- `references/stacks.md` — où chercher les unités, la doc d'API et comment vérifier, par stack.

## Phase 1 — Évaluer

### 1.1 Lancer le script

```bash
python3 <dossier-du-skill>/scripts/doc_audit.py <racine-du-projet>          # rapport markdown
python3 <dossier-du-skill>/scripts/doc_audit.py <racine-du-projet> --json   # tous les constats
```

Le script doit tourner **là où se trouve le projet**. Si le projet est sur une autre machine (dossier distant, VM, appareil relié), y copier le script dans un dossier temporaire hors du projet, jamais dans le dépôt. S'il n'y a pas de Python, faire les mêmes contrôles à la main (voir la liste en 1.3).

Il produit : inventaire, stack, unités de code et leur carte, couverture OpenAPI, déclencheurs CI, puis des constats classés **bloquant / important / mineur**, et un score sur 100.

### 1.2 Vérifier les constats du script

Le script est **heuristique**. Avant de rapporter un constat, le confirmer avec une commande ou une lecture. Faux positifs fréquents :
- chemin cité qui est un exemple, un placeholder ou une évolution envisagée (« sous-dossier `common/infra/` optionnel ») ;
- doc classée « vivante » alors qu'elle est historique (le sommaire la marque comme telle, pas le fichier) ;
- unité « absente de la carte » qui est un dossier vide ou un reliquat de migration ;
- commande citée qui existe dans un autre `package.json` ou sous un autre nom.

Écarter les faux positifs et noter les cas douteux.

### 1.3 Ce que le script ne fait pas (jugement de l'agent)

Lire en entier les points d'entrée : README, instructions d'agents (CLAUDE.md, AGENTS.md…), CONTRIBUTING, sommaire de `docs/`, index des ADR. Puis :
- **Règles énoncées en prose** (« chaque endpoint a un test », « toute décision passe par un ADR ») : en extraire les affirmations vérifiables et les tester (`grep -L`, lecture du code).
- **Exactitude** : routes, rôles, flux et variables d'environnement décrits correspondent-ils au code ?
- **CI** : avant d'écrire qu'une vérification ne tourne pas en CI, lire les déclencheurs (le script les liste). Un step dans un workflow lancé sur `pull_request` tourne bien en CI.
- **Structure** : un nouvel arrivant trouve-t-il en moins de 5 minutes comment installer, lancer, tester ? Sait-il quelle doc est à jour et laquelle est historique ?

### 1.4 Rapport (dans la conversation, pas de fichier)

1. Verdict en une ou deux phrases, avec le score et sa lettre (voir `references/severity.md`). Préciser que c'est un indicateur, pas une note absolue.
2. **Ce qui est bien** : 3 à 5 points concrets, avec les fichiers.
3. **Ce qui pose problème**, par gravité, chaque point avec sa preuve (nombre, fichiers, dates). Regrouper sous « La doc contredit le code » les écarts vérifiés et corrigeables.
4. Priorités, puis proposer de corriger les contradictions.
5. Ce qui n'a pas pu être vérifié (script non lancé, runtime absent…).

## Phase 2 — Corriger les contradictions (sur demande)

Créer une liste de tâches : une par famille d'écarts, plus une dernière « Vérifier ».

**Avant d'écrire :**
- Lire la convention du projet (guide de style, skill interne) et 2 ou 3 exemples existants : langue des descriptions, préfixes, format.
- **Mesurer les effets de bord.** Une annotation de doc peut changer un comportement : des préfixes de résumé OpenAPI ou des tags peuvent piloter le découpage de la doc par audience (admin, partenaire, client). Lire les filtres et le test correspondant. Vérifier que la correction ne fait ni apparaître ni disparaître une route d'une audience. Ne pas réutiliser un tag existant s'il fait entrer une route là où elle n'a pas sa place (ex. un webhook serveur-à-serveur dans la doc client).

**Par famille :**
- **Carte des unités** : ajouter les unités manquantes avec leurs routes et leur rôle réels, lus dans le code. Retirer les unités supprimées (`git log --diff-filter=D --format='%cd %s' -1 -- '<chemin>/*'`). Corriger les compteurs. Ignorer les dossiers vides.
- **Doc d'API manquante** : suivre `references/stacks.md` pour le décorateur ou paramètre à utiliser.
- **Chemins et liens périmés** : retrouver la nouvelle cible (`git log --follow`, recherche du nom) ; sinon retirer la référence.
- **Commandes inexistantes** : aligner sur les scripts réels, ou signaler si la commande devrait exister.
- **Fichier promis absent** (CHANGELOG, LICENSE…) : le créer au format standard. Pour un changelog, remplir « Non publié » à partir de `git log --since=… --no-merges`, sans rien inventer. Ajouter les liens depuis les docs qui l'exigent.

**Règles :**
- Modifications ciblées. Remplacer des ancres qui apparaissent exactement une fois. Préserver l'encodage et les fins de ligne.
- Ne rien supprimer sans accord (fichiers, dossiers, secrets suivis par git) : signaler et proposer.
- Si l'audit s'est trompé sur un point, le dire franchement.

### Vérifier

Dans l'ordre, s'arrêter au premier niveau qui fonctionne :
1. Commandes du projet : typecheck ou build, script de vérification de la doc, test de découpage OpenAPI.
2. Si le runtime ou les dépendances manquent : analyse syntaxique des fichiers modifiés (`typescript.transpileModule`, `python -m py_compile`, `go vet` sur le paquet, `php -l`), et contrôle que chaque symbole importé existe dans la version installée de la bibliothèque.
3. Relancer `doc_audit.py` : les constats corrigés doivent avoir disparu.

Si le niveau 1 n'a pas pu tourner, le dire en tête du compte rendu et donner les commandes exactes à lancer avant de committer.

### Compte rendu

Court : ce qui a été corrigé par famille (avec les chiffres), le score avant et après, le niveau de vérification atteint, les erreurs de l'audit initial, et ce qui reste à faire à la main.
