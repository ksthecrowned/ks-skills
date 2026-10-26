# Repères par stack

Où se trouvent les unités de code que la doc doit cartographier, comment se documente l'API, et comment vérifier une correction. Le script détecte la stack ; ce fichier sert pour la phase de correction et pour les cas qu'il ne couvre pas.

| Stack | Unités à cartographier | Doc d'API | Vérifier une correction |
|-------|------------------------|-----------|--------------------------|
| **NestJS** | `src/modules/*` | `@ApiOperation({ summary })`, `@ApiTags`, `@ApiProperty` / `@ApiPropertyOptional` sur les DTOs | `tsc --noEmit`, test e2e Swagger s'il existe |
| **Express / Fastify** | `src/routes/*`, `src/modules/*` | commentaires JSDoc `@openapi` (swagger-jsdoc) ou schémas de route Fastify | `tsc --noEmit` ou `node --check` |
| **Next.js / React** | `app/*` ou `pages/*` (routes), `src/features/*`, `components/*` | — (doc des composants : Storybook, README par feature) | `next lint`, `tsc --noEmit` |
| **FastAPI** | packages de `app/` | `summary=` / `description=` dans le décorateur, ou docstring de la fonction | `python -m py_compile`, ouvrir `/openapi.json` |
| **Django / DRF** | dossiers contenant `apps.py` | drf-spectacular : `@extend_schema(summary=…)` | `python manage.py check`, `spectacular --validate` |
| **Flask** | blueprints | flasgger / apispec : docstrings YAML | `python -m py_compile` |
| **Go** | `internal/*`, `pkg/*`, `cmd/*` | swaggo : commentaires `// @Summary` au-dessus du handler | `go vet ./...`, `swag init` si utilisé |
| **Spring Boot** | packages sous `src/main/java/<groupe>/` | springdoc : `@Operation(summary = …)` | `mvn -q compile` / `gradle compileJava` |
| **Laravel** | `app/Modules/*`, `app/Http/Controllers/*` | l5-swagger : annotations `@OA\Get` | `php -l`, `php artisan route:list` |
| **Rails** | `app/controllers/*`, engines | rswag : specs `path … do` | `bin/rails zeitwerk:check` |
| **Monorepo** (pnpm, npm workspaces, Turborepo, Nx, go.work) | `apps/*`, `packages/*`, `services/*`, `libs/*` | par app | commande de build du workspace (`turbo build --filter=…`) |

## Pièges de correction connus

- **Découpage de la doc par audience.** Des préfixes de résumé (`Admin:`, `Partner:`), des tags ou des groupes filtrent ce que chaque doc publiée affiche. Avant d'annoter, lire les filtres et le test associé.
- **Webhooks et routes internes.** Leur donner un tag dédié (`Webhooks`, `Internal`) plutôt qu'un tag métier qui les ferait apparaître dans une doc publique.
- **Réponses hors enveloppe.** Si le projet impose un format de réponse (`{ statusCode, message, data }`) et qu'une route y échappe (webhook), l'écrire dans sa description.
- **Doc générée.** Ne jamais corriger un fichier généré (`openapi.json`, `docs/api/*.md` produits par un outil) : corriger la source et relancer le générateur.
- **Docs historiques.** Ne pas « corriger » un plan ou un ADR passé pour qu'il colle au code actuel : c'est une trace. Ajouter plutôt un statut (`Superseded`, `Livré`) ou un renvoi.
