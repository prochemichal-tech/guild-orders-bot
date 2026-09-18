# Guild Orders V1

Discord bot pro guildovní objednávky ve WoW.

## Příkazy
- `/order` – vytvoření objednávky
- `/orders` – otevřené objednávky
- `/order_info` – detail objednávky

## Tlačítka
- 🙋 VZÍT ORDER
- ↩️ UVOLNIT
- ✅ SPLNĚNO – důstojník
- ❌ ZRUŠIT – zadavatel nebo důstojník

Body se ve V1 pouze uloží k objednávce; jejich připsání je ruční.

## Railway
Environment Variables:
- `DISCORD_TOKEN` = token Discord bota
- `DB_PATH` = `/data/orders.db`

Pro trvalé uložení databáze použij Railway Volume připojený na `/data`.

**Token nikdy neposílej do chatu.**
