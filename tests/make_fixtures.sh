#!/usr/bin/env bash
# Crée des projets factices avec des écarts doc <-> code connus, pour tester doc_audit.py.
# Usage : tests/make_fixtures.sh [dossier]   (défaut : /tmp/doc-audit-fixtures)
# Puis :  tests/run_fixtures.sh [dossier]
set -euo pipefail
D="${1:-/tmp/doc-audit-fixtures}"
rm -rf "$D"; mkdir -p "$D"; cd "$D"
gi(){ (cd "$1" && git init -q && git remote add origin "https://github.com/acme/$2.git" && git add -A \
       && git -c user.email=t@t -c user.name=t commit -qm init); }

# ---------- mono-dépôt : une stack par dossier -------------------------------------------
mkdir -p single && cd single

# FastAPI — attendu : make seed (bloquant), auth+billing absents, payments disparu,
#           CHANGELOG promis, lien docs/guide.md cassé, 1 route sans description
mkdir -p fastapi/app/{users,orders,billing,auth}
echo 'fastapi==0.110' > fastapi/requirements.txt
for m in users orders billing auth; do : > fastapi/app/$m/__init__.py; done
cat > fastapi/app/users/routes.py <<'EOF'
@router.get("/users")
def list_users():
    """List users."""
    return []

@router.post("/users")
def create_user(body):
    return body
EOF
cat > fastapi/app/orders/routes.py <<'EOF'
@router.get("/orders", summary="List orders")
async def list_orders():
    return []
EOF
cat > fastapi/README.md <<'EOF'
# Shop API
## Installation
Run `make dev` then `make seed`. Config in `config/settings.yaml`.
Modules: `app/users/`, `app/orders/`, `app/payments/`.
See [guide](docs/guide.md) and the CHANGELOG.
EOF
printf 'dev:\n\tuvicorn app.main:app\n' > fastapi/Makefile
gi fastapi fastapi

# Django — attendu : app accounts jamais citée (mineur)
mkdir -p django/shop/{catalog,cart,accounts}
echo 'Django>=5' > django/requirements.txt
for m in catalog cart accounts; do echo "class C: pass" > django/shop/$m/apps.py; done
printf '# Shop\n## Getting started\nApps : `catalog`, `cart`.\n' > django/README.md
gi django django

# Go — attendu : .env suivi (bloquant), queue absent, cache disparu
mkdir -p go/internal/{store,http,queue} go/cmd/api
echo 'module x' > go/go.mod
for m in store http queue; do echo 'package p' > go/internal/$m/a.go; done
echo 'package main' > go/cmd/api/main.go
printf '# Go svc\n\n## Quick start\n`just run`\n\n## Packages\n- `internal/store/`\n- `internal/http/`\n- `internal/cache/`\n' > go/README.md
printf 'run:\n    go run ./cmd/api\n' > go/justfile
echo 'SECRET=1' > go/.env
gi go go

# Monorepo pnpm — attendu : pnpm run test:e2e (bloquant), admin + config absents,
#                 legacy disparu, chemin absolu D:\Work (dépendance externe)
mkdir -p mono/apps/{web,admin} mono/packages/{ui,utils,config}
echo '{"name":"m","scripts":{"build":"turbo build"},"workspaces":["apps/*","packages/*"]}' > mono/package.json
echo 'packages: ["apps/*","packages/*"]' > mono/pnpm-workspace.yaml
for d in apps/web apps/admin packages/ui packages/utils packages/config; do echo 'export {}' > mono/$d/index.ts; done
echo '{"name":"web","scripts":{"dev":"next dev"}}' > mono/apps/web/package.json
printf '# Mono\n## Setup\n`pnpm run dev`, `pnpm run test:e2e`\n\n| Package | Path |\n|--|--|\n| UI | `packages/ui/` |\n| Utils | `packages/utils/` |\n| Web | `apps/web/` |\n| Old | `packages/legacy/` |\n' > mono/README.md
printf '# Contribuer\nVoir le guide dans `D:\\\\Work\\\\handbook\\\\index.md`.\n' > mono/CONTRIBUTING.md
gi mono mono

# Laravel — attendu : modules jamais cités, README sans section de démarrage
mkdir -p laravel/app/Modules/{Billing,Users}
echo '{"require":{"laravel/framework":"^11"}}' > laravel/composer.json
echo '<?php' > laravel/app/Modules/Billing/A.php; echo '<?php' > laravel/app/Modules/Users/A.php
printf '# App\n' > laravel/README.md
gi laravel laravel
cd ..

# ---------- écosystème : hub + API + app + dépôt sans lien --------------------------------
mkdir -p eco && cd eco
mkdir -p platform-docs/{projects,integrations,reference}
cat > platform-docs/INDEX.md <<'EOF'
# Platform hub
| Projet | Path |
|--|--|
| shop-api | `D:\Projects\shop-api` |
| shop-app | `D:\shop-app` |
Lire `integrations/payments.md`.
EOF
cat > platform-docs/integrations/payments.md <<'EOF'
# Paiements
Le client appelle `POST /api/payments` puis attend le webhook de confirmation du prestataire avant de livrer.
Toutes les réponses suivent l'enveloppe standard avec statusCode, message et data dans le corps JSON.
Les erreurs de paiement renvoient un code métier explicite que les apps traduisent pour l'utilisateur final.
Le remboursement passe toujours par le back-office et jamais directement depuis l'application mobile client.
Les montants sont exprimés en centimes entiers pour éviter toute erreur d'arrondi entre les services.
Voir le code : `shop-api/src/modules/pay/` et `shop-api/src/modules/billing/`.
EOF
echo "# Auth" > platform-docs/integrations/auth.md
cat > platform-docs/projects/shop-api.md <<'EOF'
# shop-api
Carte complète ~9 modules.
## Modules
`auth`, `orders`, `pay`, `coupons`, `legacy-cart`
## Entrées
| Chemin | Rôle |
|--|--|
| `src/modules/pay/` | Paiement |
| `src/modules/old-reports/` | Rapports |
EOF
printf '# Skills\n## shop-api (2 fichiers)\n`a.md`\n## shop-app (3 fichiers)\n' > platform-docs/reference/skills-by-repo.md
gi platform-docs platform-docs

mkdir -p shop-api/src/modules/{auth,orders,pay,stats} shop-api/.skills
for m in auth orders pay stats; do echo 'export {}' > shop-api/src/modules/$m/$m.module.ts; done
for s in arch api swagger testing; do printf -- "---\nstatus: stable\n---\n# $s\n" > shop-api/.skills/$s.md; done
echo '{"name":"shop-api","scripts":{"dev":"x"}}' > shop-api/package.json
cat > shop-api/README.md <<'EOF'
# shop-api
## Démarrage
`npm run dev`
Hub : `D:\Projects\platform-docs\INDEX.md` · `platform-docs/integrations/payments.md` · `platform-docs/integrations/refunds.md`
Voir aussi `integrations/auth.md` et `integrations/sse.md`.
Code : `D:/Projects/shop-api/src/modules/auth/auth.module.ts`.
## Paiements (copie)
Le client appelle `POST /api/payments` puis attend le webhook de confirmation du prestataire avant de livrer.
Toutes les réponses suivent l'enveloppe standard avec statusCode, message et data dans le corps JSON.
Les erreurs de paiement renvoient un code métier explicite que les apps traduisent pour l'utilisateur final.
Le remboursement passe toujours par le back-office et jamais directement depuis l'application mobile client.
Les montants sont exprimés en centimes entiers pour éviter toute erreur d'arrondi entre les services.
EOF
gi shop-api shop-api

# dossier local nommé autrement que le remote (cas réel : ride-client -> remote ride-congo)
mkdir -p mobile-client/.skills mobile-client/app
echo 'export {}' > mobile-client/app/index.ts
for s in api nav ui; do echo "# $s" > mobile-client/.skills/$s.md; done
cat > mobile-client/README.md <<'EOF'
# shop-app
## Setup
Backend : `D:/Projects/shop-api/src/modules/orders/orders.module.ts`, `D:/Projects/shop-api/src/modules/legacy/cart.ts`
Contrat : [paiements](../platform-docs/integrations/payments.md), [webhooks](../platform-docs/integrations/webhooks.md)
EOF
gi mobile-client shop-app

mkdir -p unrelated/src && echo 'x' > unrelated/src/a.ts && echo '# Unrelated' > unrelated/README.md
gi unrelated unrelated
echo "Fixtures créées dans $D"
