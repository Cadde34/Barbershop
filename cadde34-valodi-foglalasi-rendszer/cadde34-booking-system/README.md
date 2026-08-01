# CADDE 34 – valódi, közös időpontfoglaló rendszer

Ez a csomag a kijavított CADDE 34 weboldalt, egy SQLite-adatbázist használó foglalási szervert, automatikus ütközésvédelmet és egy jelszóval védett admin felületet tartalmaz.

A weboldal nem egyszerű statikus HTML többé: a vendégek ugyanazt a közös foglalási adatbázist használják, ezért egy már lefoglalt vagy átfedő időpontot más nem tud újra lefoglalni.

## Mit tartalmaz?

- Magyar–angol weboldal a meglévő megjelenés megtartásával.
- Pontosan a négy hivatalos szolgáltatás:
  - Hajvágás – 7 000 Ft
  - Stílusvágás – 8 000 Ft
  - Szakáll – 4 000 Ft
  - Haj + szakáll – 10 000 Ft
- 30 perces kezdési időpontok.
- Valós idejű szabad/foglalt időponttábla.
- SQLite-adatbázis, amely minden vendég számára közös.
- Tranzakciós ütközésvédelem: egyszerre érkező foglalások közül ugyanarra az időre csak egy menthető el.
- A szolgáltatás teljes időtartamának blokkolása. Például egy 60 perces foglalás a köztes 30 perces kezdési időpontot is kizárja.
- Jelszóval védett admin felület a foglalások és a szolgáltatási időtartamok kezeléséhez.
- Lemondás, teljesítés és „nem jelent meg” állapotok.
- Biztonságos visszaállítás: egy lemondott foglalás nem aktiválható újra, ha az időpontot közben más lefoglalta.
- Opcionális automatikus SMS a szalonnak Twilio-integrációval.
- WhatsApp-üzenet gomb tartalék megoldásként.

## Gyors indítás Windows alatt

1. Csomagold ki a ZIP-fájlt egy normál mappába.
2. Kattints duplán a `start.bat` fájlra.
3. A weboldal megnyílik itt: `http://localhost:8000/`
4. Az admin felület itt érhető el: `http://localhost:8000/admin`

Python 3.10 vagy újabb szükséges. Külső Python-csomagot nem kell telepíteni.

## Indítás macOS vagy Linux alatt

A projekt mappájában futtasd:

```bash
chmod +x start.sh
./start.sh
```

Vagy közvetlenül:

```bash
python3 server.py
```

## Admin belépés

- Felhasználónév: `admin`
- Jelszó: `C34-Pa6Mhp768npmqDhO!`

Éles közzététel előtt mindenképpen változtasd meg a jelszót. Ezt kétféleképpen teheted meg:

1. módosítod a `config.json` fájl `admin_password` értékét; vagy
2. létrehozol egy `.env` fájlt a `.env.example` alapján, és beállítod a `CADDE34_ADMIN_PASSWORD` változót.

A környezeti változó elsőbbséget élvez a `config.json` fájllal szemben.

## Hol tárolódnak a foglalások?

Az első indításkor automatikusan létrejön:

```text
data/bookings.db
```

Ez a közös SQLite-adatbázis. Ne töröld, és éles használatnál készíts róla rendszeres biztonsági másolatot. A tárhelynek tartósan meg kell őriznie a `data` mappát.

## Szolgáltatási időtartamok

A pontos szolgáltatási időtartamokat a rendelkezésre álló adatok nem tartalmazták, ezért induláskor mind a négy szolgáltatás 30 percet foglal.

Az admin felületen külön-külön beállítható 30, 60, 90, 120, 150, 180, 210 vagy 240 perc. A módosítás az új foglalások elérhetőségére azonnal érvényes, a már rögzített foglalások pedig megtartják az eredeti időtartamukat és árukat.

## Automatikus SMS a szalonnak

A foglalás minden esetben először bekerül az adatbázisba. Csak ezután indul el az értesítés, ezért SMS-hiba esetén sem vész el a foglalás.

Automatikus SMS-hez:

1. Másold a `.env.example` fájlt `.env` néven.
2. Töltsd ki:

```text
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=...
CADDE34_NOTIFICATION_TO=+36705910745
```

A `TWILIO_FROM_NUMBER` helyett használható `TWILIO_MESSAGING_SERVICE_SID` is.

A rendszer csak azt jelzi „elküldöttnek”, amit a Twilio API sikeresen elfogadott; nem állítja tévesen, hogy a telefon biztosan kézbesítette. Az értesítés állapota és az esetleges hiba az admin felületen látható, az SMS pedig onnan újraküldhető.

Twilio-adatok nélkül is működik a teljes foglalási rendszer. Ilyenkor a visszaigazolásnál megjelenik egy előre kitöltött WhatsApp-gomb, amellyel a foglalás adatai elküldhetők a `+36 70 591 0745` számra.

## Beállítások

A `config.json` fájlban módosítható:

- szerver címe és portja;
- admin felhasználónév és jelszó;
- nyitási és zárási idő;
- nyitvatartási napok (`0` = hétfő, `6` = vasárnap);
- időpontok lépésköze;
- minimális előfoglalási idő;
- legfeljebb hány napra előre lehet foglalni;
- értesítési telefonszám.

## Fontos használati megjegyzés

A `static/index.html` fájlt ne nyisd meg közvetlenül dupla kattintással. A több felhasználó között közös foglalásérzékeléshez mindig a `server.py` fájlt kell futtatni, és a weboldalt a `http://localhost:8000/` címen kell megnyitni.

Ahhoz, hogy az interneten minden vendég ugyanazt a foglalási rendszert használja, a teljes projektet egy folyamatosan futó Python- vagy Docker-tárhelyre kell telepíteni. Pusztán az HTML-fájl feltöltése nem elegendő.

## Dockeres indítás

1. Másold a `.env.example` fájlt `.env` néven, és állíts be erős admin jelszót.
2. Futtasd:

```bash
docker compose up -d --build
```

A `docker-compose.yml` a helyi `data` mappát csatolja be, így az adatbázis újraindítás után is megmarad.

## Automatikus teszt

Futtasd:

```bash
python3 test_booking_system.py
```

A teszt ellenőrzi többek között, hogy:

- csak a négy hivatalos szolgáltatás érhető el;
- az azonos időpont másodszor nem foglalható;
- egy hosszabb szolgáltatás minden átfedő időpontot blokkol;
- két egyidejű foglalási kísérletből csak az egyik sikerül;
- a lemondás felszabadítja az időpontot;
- egy közben újrafoglalt időpontra a régi foglalás nem állítható vissza.

## Éles üzemeltetés előtt

Szükséges legalább:

- HTTPS;
- erős, egyedi admin jelszó;
- tartós tárhely a `data/bookings.db` fájlnak;
- rendszeres adatbázis-mentés;
- folyamatosan futó szerverfolyamat;
- megfelelő adatkezelési és adatvédelmi tájékoztató;
- a valós szolgáltatási időtartamok beállítása az admin oldalon.
