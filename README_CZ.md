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

## Potvrzení pravidel (Trial)
Nový soubor `rules_verify.py` přidává automatické ověření pravidel.

1. Kanál pojmenuj `potvrzeni-pravidel` nebo `potvrzení-pravidel`.
2. Role musí být `Trial` nebo `🌱 Trial`.
3. Role bota musí být v pořadí rolí NAD rolí Trial.
4. Bot potřebuje oprávnění `Manage Roles` a `Manage Messages`.
5. V Discord Developer Portal -> Bot zapni **Message Content Intent**.
6. Hráč do potvrzovacího kanálu napíše `Souhlasím`.
7. Bot přidělí Trial, smaže potvrzovací zprávu a zkusí hráči poslat soukromé potvrzení.

Funkce je oddělená od Guild Orders v samostatném souboru; původní příkazy zůstávají zachované.
