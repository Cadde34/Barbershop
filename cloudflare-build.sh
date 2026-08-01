#!/usr/bin/env bash
set -euo pipefail

# Cloudflare Pages only uploads this generated directory.
# Keeping the output separate prevents Python, database and development files
# from becoming public web assets.
rm -rf public
mkdir -p public

cp index.html public/index.html
cp _worker.js public/_worker.js
cp cadde34-valodi-foglalasi-rendszer/cadde34-booking-system/static/admin.html public/admin.html

# Correct the old local-server wording in the admin page for Cloudflare D1.
sed -i 's/közös SQLite-adatbázisból/közös Cloudflare D1 adatbázisból/g' public/admin.html
sed -i 's#az admin jelszót a <code>\.env</code> fájlban vagy a <code>CADDE34_ADMIN_PASSWORD</code> környezeti változóval#az admin jelszót a Cloudflare projekt <code>ADMIN_PASSWORD</code> titkos változójával#g' public/admin.html

printf 'Cloudflare Pages output prepared in ./public\n'
