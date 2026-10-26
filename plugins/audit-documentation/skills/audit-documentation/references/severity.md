# Barème de gravité

Le même barème sert au script (`doc_audit.py`) et au rapport de l'agent, pour que deux audits restent comparables.

## Niveaux

| Niveau | Définition | Exemples |
|--------|-----------|----------|
| **Bloquant** | Quelqu'un qui suit la doc échoue, ou la doc expose un risque. | Pas de README ; commande du README qui n'existe pas ; secret (`.env`, clé de service) suivi par git. |
| **Important** | La doc affirme quelque chose de faux sur le code, ou dépend de ce que le dépôt ne contient pas. | Unité absente de la carte des modules ; carte qui cite une unité supprimée ; chemin cité disparu dans une doc à jour ; lien relatif cassé ; commande citée inexistante ailleurs que dans le README ; fichier promis absent (CHANGELOG exigé par CONTRIBUTING) ; contrôleur sans aucune description OpenAPI ; chemin absolu vers une autre machine. |
| **Mineur** | Dette qui fera dériver la doc, sans erreur immédiate. | Consignes recopiées dans plusieurs fichiers ; dates de vérification posées en masse ; doc non touchée depuis longtemps alors que le code a bougé ; specs sans statut ; routes partiellement décrites ; README sans section de démarrage ; chemins périmés dans des docs historiques. |

Un constat de l'agent hors script se classe avec les mêmes définitions.

## Score

`score = 100 − Σ pénalités`. Chaque niveau a un plafond, pour qu'une seule famille de problèmes très nombreux n'écrase pas la note :

| Niveau | Pénalité par constat | Plafond |
|--------|---------------------|---------|
| Bloquant | 15 | 60 |
| Important | 5 | 40 |
| Mineur | 1 | 10 |

| Score | Lettre | Lecture |
|-------|--------|---------|
| 90–100 | A | Fiable : on peut suivre la doc sans vérifier. |
| 75–89 | B | Bonne, quelques écarts localisés. |
| 60–74 | C | Utile, mais à recouper avec le code. |
| 40–59 | D | Dérive notable : plusieurs affirmations fausses. |
| 0–39 | E | Trompeuse ou absente. |

Le score mesure la **cohérence doc ↔ code** détectable automatiquement, pas la qualité rédactionnelle. Une doc très riche peut avoir un score moyen à cause de sa taille : le rapporter avec les constats, pas seul.
