# CADDE 34 – Cloudflare Pages + D1 telepítés

A repository Cloudflare Pages-re van előkészítve. A weboldal, az adminfelület és a D1-alapú foglalási backend a `cloudflare-build.sh` futtatásával kerül a `public` buildkönyvtárba.

## Cloudflare Pages buildbeállítások

- Production branch: `main`
- Framework preset: `None`
- Root directory: hagyd üresen
- Build command: `bash cloudflare-build.sh`
- Build output directory: `public`

## D1 adatbázis

1. Hozz létre egy D1 adatbázist `cadde34-bookings` néven.
2. A Pages projektben nyisd meg a `Settings > Bindings` részt.
3. Adj hozzá egy D1 bindingot pontosan ezzel a változónévvel: `DB`.
4. Válaszd ki a `cadde34-bookings` adatbázist.
5. Indíts új deploymentet.

A táblákat és a négy hivatalos szolgáltatást a Worker az első API-kéréskor automatikusan létrehozza.

## Kötelező változók és titkok

A Pages projekt `Settings > Variables and Secrets` részében add meg Production környezethez:

- `ADMIN_USER` = `admin`
- `ADMIN_PASSWORD` = egy erős, egyedi jelszó; titkosított secretként add meg
- `CADDE34_NOTIFICATION_TO` = `+36705910745`

SMS nélkül is működik a foglalás és a dupla foglalás elleni védelem. Automatikus Twilio SMS-hez opcionálisan szükséges:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN` – secret
- `TWILIO_FROM_NUMBER`

vagy a küldő telefonszám helyett:

- `TWILIO_MESSAGING_SERVICE_SID`

## Ellenőrzés

- Weboldal: `https://<projekt>.pages.dev/`
- Admin: `https://<projekt>.pages.dev/admin`
- Állapotellenőrzés: `https://<projekt>.pages.dev/api/health`

A helyes health válaszban szerepelnie kell ennek:

```json
{
  "ok": true,
  "database": "cloudflare-d1"
}
```

Ha az időpontok nem töltődnek be, először ellenőrizd, hogy a D1 binding neve pontosan `DB`, majd indíts új deploymentet.
