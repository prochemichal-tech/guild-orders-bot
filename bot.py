import os, sqlite3
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = os.getenv('DB_PATH', '/data/orders.db')
WEEKLY_POINT_LIMIT = int(os.getenv('WEEKLY_POINT_LIMIT', '20'))
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '1550453693445308487'))
if not TOKEN:
    raise RuntimeError('Chybí proměnná DISCORD_TOKEN.')
os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        reward INTEGER NOT NULL,
        creator_id INTEGER NOT NULL,
        claimer_id INTEGER,
        status TEXT NOT NULL DEFAULT 'OPEN',
        channel_id INTEGER,
        message_id INTEGER,
        created_at TEXT NOT NULL,
        claimed_at TEXT,
        completed_at TEXT,
        cancelled_at TEXT,
        reviewed_by INTEGER,
        reviewed_at TEXT,
        guild_slot INTEGER,
        control_slot INTEGER
    )''')
    # Migration for an existing V1 database.
    cols = {r['name'] for r in c.execute('PRAGMA table_info(orders)').fetchall()}
    for name, typ in [('reviewed_by', 'INTEGER'), ('reviewed_at', 'TEXT'), ('guild_slot', 'INTEGER'), ('control_slot', 'INTEGER')]:
        if name not in cols:
            c.execute(f'ALTER TABLE orders ADD COLUMN {name} {typ}')
    c.execute('''CREATE TABLE IF NOT EXISTS inventory (
        item TEXT PRIMARY KEY,
        quantity INTEGER NOT NULL DEFAULT 0,
        updated_by INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        item TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    c.commit()
    c.close()

def get_order(i):
    c = db(); r = c.execute('SELECT * FROM orders WHERE id=?', (i,)).fetchone(); c.close(); return r


def _week_start_utc():
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def weekly_points(user_id):
    """Body získané + rezervované v aktuálním týdnu (pondělí–neděle)."""
    start = _week_start_utc().isoformat()
    c = db()
    r = c.execute("""
        SELECT COALESCE(SUM(reward),0) AS total FROM orders
        WHERE claimer_id=?
          AND status IN ('CLAIMED','PENDING_REVIEW','COMPLETED')
          AND (
            (status='COMPLETED' AND reviewed_at>=?)
            OR
            (status!='COMPLETED' AND claimed_at>=?)
          )
    """,(user_id,start,start)).fetchone()
    c.close()
    return int(r['total'] or 0)

def create_order(item, q, reward, uid, cid):
    c = db()
    cur = c.execute(
        'INSERT INTO orders(item,quantity,reward,creator_id,channel_id,created_at) VALUES(?,?,?,?,?,?)',
        (item, q, reward, uid, cid, datetime.now(timezone.utc).isoformat())
    )
    i = cur.lastrowid
    c.commit(); c.close(); return i

def set_message(i, mid):
    c = db(); c.execute('UPDATE orders SET message_id=? WHERE id=?', (mid, i)); c.commit(); c.close()

def is_staff(m):
    return isinstance(m, discord.Member) and (m.guild_permissions.manage_guild or m.guild_permissions.administrator)

def status_text(s):
    return {
        'OPEN': '🟢 VOLNÉ',
        'CLAIMED': '🟡 VYŘIZUJE',
        'PENDING_REVIEW': '🟠 ČEKÁ NA KONTROLU',
        'COMPLETED': '✅ SPLNĚNO',
        'CANCELLED': '❌ ZRUŠENO'
    }.get(s, s)

async def uname(g, uid):
    if not uid:
        return '—'
    m = g.get_member(uid)
    if m:
        return m.display_name
    try:
        return (await g.fetch_member(uid)).display_name
    except Exception:
        return f'ID {uid}'

async def embed_for(g, o):
    col = {
        'OPEN': discord.Color.gold(),
        'CLAIMED': discord.Color.orange(),
        'PENDING_REVIEW': discord.Color.dark_orange(),
        'COMPLETED': discord.Color.green(),
        'CANCELLED': discord.Color.red()
    }[o['status']]
    e = discord.Embed(title=f"📦 ORDER #{o['id']:03d}", color=col)
    e.add_field(name='Předmět', value=o['item'], inline=False)
    e.add_field(name='Množství', value=str(o['quantity']), inline=True)
    e.add_field(name='Odměna', value=f"**{o['reward']} bodů**", inline=True)
    e.add_field(name='Stav', value=status_text(o['status']), inline=False)
    e.add_field(name='Zadal', value=await uname(g, o['creator_id']), inline=True)
    if o['claimer_id']:
        e.add_field(name='Vyřizuje', value=await uname(g, o['claimer_id']), inline=True)
    if o['status'] == 'PENDING_REVIEW':
        e.add_field(name='Kontrola', value='Čeká na potvrzení důstojníkem / vedením.', inline=False)
    if o['guild_slot']:
        e.add_field(name='Guild slot', value=f'#{o["guild_slot"]}', inline=True)
    if o['status'] == 'COMPLETED' and o['reviewed_by']:
        e.add_field(name='Schválil', value=await uname(g, o['reviewed_by']), inline=True)
    if o['control_slot']:
        e.add_field(name='Kontrolní slot', value=f'#{o["control_slot"]}', inline=True)
    e.set_footer(text='Guild Orders • body se zatím připisují ručně')
    return e

def update_status(i, status, uid=None, reviewer=None, guild_slot=None, control_slot=None):
    c = db(); now = datetime.now(timezone.utc).isoformat()
    if status == 'CLAIMED':
        cur = c.execute("UPDATE orders SET claimer_id=?,status='CLAIMED',claimed_at=? WHERE id=? AND status='OPEN'", (uid, now, i))
    elif status == 'OPEN':
        cur = c.execute("UPDATE orders SET claimer_id=NULL,status='OPEN',claimed_at=NULL WHERE id=? AND status='CLAIMED'", (i,))
    elif status == 'PENDING_REVIEW':
        cur = c.execute("UPDATE orders SET status='PENDING_REVIEW',completed_at=?,guild_slot=? WHERE id=? AND status='CLAIMED' AND claimer_id=?", (now, guild_slot, i, uid))
    elif status == 'COMPLETED':
        cur = c.execute("UPDATE orders SET status='COMPLETED',reviewed_by=?,reviewed_at=?,control_slot=? WHERE id=? AND status='PENDING_REVIEW'", (reviewer, now, control_slot, i))
    elif status == 'CLAIMED_FROM_REVIEW':
        cur = c.execute("UPDATE orders SET status='CLAIMED' WHERE id=? AND status='PENDING_REVIEW'", (i,))
    else:
        cur = c.execute("UPDATE orders SET status='CANCELLED',cancelled_at=? WHERE id=? AND status NOT IN ('COMPLETED','CANCELLED')", (now, i))
    ok = cur.rowcount > 0
    c.commit(); c.close(); return ok

class GuildSlotView(discord.ui.View):
    def __init__(self, order_id, user_id):
        super().__init__(timeout=120)
        self.order_id = order_id
        self.user_id = user_id
        options = [discord.SelectOption(label=f'Guild slot {i}', value=str(i)) for i in range(1, 3)]
        select = discord.ui.Select(placeholder='Vyber guild slot 1–2', options=options, custom_id=f'guildorder:slot:{order_id}')
        async def callback(interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message('❌ Tento výběr patří hráči, který order vyřizuje.', ephemeral=True)
            slot = int(select.values[0])
            if not update_status(self.order_id, 'PENDING_REVIEW', self.user_id, guild_slot=slot):
                return await interaction.response.edit_message(content='❌ Order už není ve stavu VYŘIZUJE.', view=None)
            o = get_order(self.order_id)
            await interaction.response.edit_message(content=f'✅ Order označen jako splněný. Guild slot: **#{slot}**. Čeká na kontrolu důstojníkem / vedením.', view=None)
            channel = interaction.channel
            try:
                msg = await channel.fetch_message(o['message_id'])
                await msg.edit(embed=await embed_for(interaction.guild, o), view=OrderView(self.order_id))
            except Exception:
                pass
            self.stop()
        select.callback = callback
        self.add_item(select)

class ControlSlotView(discord.ui.View):
    def __init__(self, order_id, reviewer_id):
        super().__init__(timeout=120)
        self.order_id = order_id
        self.reviewer_id = reviewer_id
        options = [discord.SelectOption(label=f'Kontrolní slot {i}', value=str(i)) for i in range(1, 9)]
        select = discord.ui.Select(placeholder='Vyber kontrolní slot 1–8', options=options, custom_id=f'guildorder:controlslot:{order_id}')
        async def callback(interaction):
            if interaction.user.id != self.reviewer_id:
                return await interaction.response.send_message('❌ Tento výběr patří důstojníkovi / vedení, které kontrolu spustilo.', ephemeral=True)
            slot = int(select.values[0])
            if not update_status(self.order_id, 'COMPLETED', reviewer=interaction.user.id, control_slot=slot):
                return await interaction.response.edit_message(content='❌ Order už není ve stavu ČEKÁ NA KONTROLU.', view=None)
            o = get_order(self.order_id)

            # Schválený order = dodané kusy se automaticky přičtou do inventáře guild banky.
            inv = get_inventory(o['item'])
            if inv:
                new_inventory_quantity = change_inventory(o['item'], o['quantity'], interaction.user.id)
            else:
                set_inventory(o['item'], o['quantity'], interaction.user.id)
                new_inventory_quantity = o['quantity']

            await interaction.response.edit_message(
                content=(
                    f'✅ Order potvrzen. Kontrolní slot: **#{slot}**. '
                    f'Do inventáře bylo přidáno **{o["quantity"]}× {o["item"]}** '
                    f'(nový stav: **{new_inventory_quantity} ks**). Body lze nyní ručně doplnit.'
                ),
                view=None
            )

            # Soukromá zpráva hráči, který order vyřídil.
            try:
                member = interaction.guild.get_member(o['claimer_id'])
                if member is None:
                    member = await interaction.guild.fetch_member(o['claimer_id'])
                dm = discord.Embed(
                    title=f'✅ ORDER #{o["id"]:03d} SCHVÁLEN',
                    description='Tvůj order byl schválen vedením guildy.',
                    color=discord.Color.green()
                )
                dm.add_field(name='Předmět', value=o['item'], inline=False)
                dm.add_field(name='Množství', value=str(o['quantity']), inline=True)
                dm.add_field(name='Odměna', value=f'**{o["reward"]} bodů**', inline=True)
                dm.add_field(name='Informace', value='Body ti budou připsány.', inline=False)
                dm.set_footer(text='Guild Orders')
                await member.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                # Hráč může mít soukromé zprávy ze serveru vypnuté.
                pass

            try:
                msg = await interaction.channel.fetch_message(o['message_id'])
                await msg.edit(embed=await embed_for(interaction.guild, o), view=OrderView(self.order_id))
            except Exception:
                pass
            self.stop()
        select.callback = callback
        self.add_item(select)

class OrderView(discord.ui.View):
    def __init__(self, i):
        super().__init__(timeout=None)
        self.i = i
        o = get_order(i)
        status = o['status'] if o else 'CANCELLED'

        if status == 'OPEN':
            self.add_item(self._button('🙋', 'VZÍT ORDER', discord.ButtonStyle.success, 'claim'))
            self.add_item(self._button('❌', 'ZRUŠIT', discord.ButtonStyle.danger, 'cancel'))
        elif status == 'CLAIMED':
            self.add_item(self._button('↩️', 'UVOLNIT', discord.ButtonStyle.secondary, 'release'))
            self.add_item(self._button('✅', 'OZNAČIT JAKO SPLNĚNÉ', discord.ButtonStyle.primary, 'complete_request'))
            self.add_item(self._button('❌', 'ZRUŠIT', discord.ButtonStyle.danger, 'cancel'))
        elif status == 'PENDING_REVIEW':
            self.add_item(self._button('👍', 'POTVRDIT A UDĚLIT BODY', discord.ButtonStyle.success, 'approve'))
            self.add_item(self._button('↩️', 'VRÁTIT K VYŘIZOVÁNÍ', discord.ButtonStyle.secondary, 'return_to_claimed'))
            self.add_item(self._button('❌', 'ZRUŠIT', discord.ButtonStyle.danger, 'cancel'))

    def _button(self, emoji, label, style, kind):
        b = discord.ui.Button(emoji=emoji, label=label, style=style, custom_id=f'guildorder:{kind}:{self.i}')
        b.callback = getattr(self, kind)
        return b

    async def redraw(self, interaction):
        o = get_order(self.i)
        await interaction.response.edit_message(embed=await embed_for(interaction.guild, o), view=OrderView(self.i))

    async def claim(self, interaction):
        o = get_order(self.i)
        if not o or o['status'] != 'OPEN':
            return await interaction.response.send_message('❌ Objednávka už není volná.', ephemeral=True)

        used = weekly_points(interaction.user.id)
        if used + o['reward'] > WEEKLY_POINT_LIMIT:
            return await interaction.response.send_message(
                f'❌ Tento order nemůžeš vzít. Tento týden máš získáno nebo rezervováno '
                f'**{used}/{WEEKLY_POINT_LIMIT} bodů** a tento order je za **{o["reward"]} bodů**. '
                f'Překročil bys týdenní limit.',
                ephemeral=True
            )

        if not update_status(self.i, 'CLAIMED', interaction.user.id):
            return await interaction.response.send_message('❌ Objednávka už není volná.', ephemeral=True)
        await self.redraw(interaction)

    async def release(self, interaction):
        o = get_order(self.i)
        if not o or o['status'] != 'CLAIMED' or (o['claimer_id'] != interaction.user.id and not is_staff(interaction.user)):
            return await interaction.response.send_message('❌ Tuto objednávku může uvolnit pouze její řešitel nebo důstojník.', ephemeral=True)
        update_status(self.i, 'OPEN'); await self.redraw(interaction)

    async def complete_request(self, interaction):
        o = get_order(self.i)
        if not o or o['status'] != 'CLAIMED' or o['claimer_id'] != interaction.user.id:
            return await interaction.response.send_message('❌ Označit order jako splněný může pouze hráč, který ho právě vyřizuje.', ephemeral=True)
        view = GuildSlotView(self.i, interaction.user.id)
        await interaction.response.send_message('📦 Do kterého guild slotu byl order předán?', view=view, ephemeral=True)

    async def approve(self, interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('❌ Potvrdit splnění a udělit body může pouze důstojník / vedení.', ephemeral=True)
        await interaction.response.send_message('📋 Do kterého kontrolního slotu důstojník / vedení order přeřadilo?', view=ControlSlotView(self.i, interaction.user.id), ephemeral=True)

    async def return_to_claimed(self, interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('❌ Vrátit order k vyřízení může pouze důstojník / vedení.', ephemeral=True)
        if not update_status(self.i, 'CLAIMED_FROM_REVIEW'):
            return await interaction.response.send_message('❌ Order už není ve stavu ČEKÁ NA KONTROLU.', ephemeral=True)
        await self.redraw(interaction)

    async def cancel(self, interaction):
        o = get_order(self.i)
        if not o or (o['creator_id'] != interaction.user.id and not is_staff(interaction.user)):
            return await interaction.response.send_message('❌ Zrušit ji může zadavatel nebo důstojník.', ephemeral=True)
        if not update_status(self.i, 'CANCELLED'):
            return await interaction.response.send_message('❌ Objednávku už nelze zrušit.', ephemeral=True)
        await self.redraw(interaction)

def normalize_item(s):
    return ' '.join(s.strip().split()).casefold()

def get_inventory(item):
    c = db(); r = c.execute('SELECT * FROM inventory WHERE item=?', (normalize_item(item),)).fetchone(); c.close(); return r

def set_inventory(item, quantity, uid):
    key = normalize_item(item); now = datetime.now(timezone.utc).isoformat(); c = db()
    c.execute('INSERT INTO inventory(item,quantity,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(item) DO UPDATE SET quantity=excluded.quantity,updated_by=excluded.updated_by,updated_at=excluded.updated_at', (key, quantity, uid, now))
    c.commit(); c.close()

def change_inventory(item, delta, uid):
    key = normalize_item(item); now = datetime.now(timezone.utc).isoformat(); c = db()
    r = c.execute('SELECT quantity FROM inventory WHERE item=?', (key,)).fetchone()
    if not r: c.close(); return None
    newq = r['quantity'] + delta
    if newq < 0: c.close(); return None
    c.execute('UPDATE inventory SET quantity=?,updated_by=?,updated_at=? WHERE item=?', (newq, uid, now, key)); c.commit(); c.close(); return newq

def create_item_log(player_id, item, quantity, reason, created_by):
    c = db(); cur = c.execute('INSERT INTO items(player_id,item,quantity,reason,created_by,created_at) VALUES(?,?,?,?,?,?)',
                    (player_id, item.strip(), quantity, reason.strip(), created_by, datetime.now(timezone.utc).isoformat()))
    i = cur.lastrowid; c.commit(); c.close(); return i

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default())

    async def setup_hook(self):


        guild = discord.Object(id=GUILD_ID)


        # Copy global commands to the main guild and sync them immediately.


        self.tree.copy_global_to(guild=guild)


        synced = await self.tree.sync(guild=guild)


        print(f'Synchronizováno {len(synced)} příkazů na server {GUILD_ID}.')


    async def on_ready(self):
        print(f'Guild Orders online: {self.user} ({self.user.id})')

bot = Bot()

@bot.tree.command(name='item', description='Ruční zápis vydaného guild itemu hráči')
@app_commands.describe(player='Hráč, který item dostal', item='Název itemu', quantity='Počet kusů', reason='Proč hráč item dostal')
async def item(interaction: discord.Interaction, player: discord.Member, item: str, quantity: int, reason: str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento zápis může dělat pouze důstojník / vedení.', ephemeral=True)
    if quantity < 1 or not item.strip() or len(item) > 200 or not reason.strip() or len(reason) > 500:
        return await interaction.response.send_message('❌ Zkontroluj item, počet kusů a důvod.', ephemeral=True)
    inv = get_inventory(item)
    if not inv:
        return await interaction.response.send_message('❌ Tento item zatím není v inventáři. Nejprve ho přidej přes `/stock` s počátečním množstvím.', ephemeral=True)
    if inv['quantity'] < quantity:
        return await interaction.response.send_message(f'❌ V inventáři je jen **{inv["quantity"]} ks** tohoto itemu, nelze vydat {quantity} ks.', ephemeral=True)
    newq = change_inventory(item, -quantity, interaction.user.id)
    item_id = create_item_log(player.id, item.strip(), quantity, reason.strip(), interaction.user.id)
    e = discord.Embed(title=f'🎁 GB ITEM #{item_id:03d}', color=discord.Color.blurple())
    e.add_field(name='Hráč', value=player.mention, inline=True)
    e.add_field(name='Item', value=item.strip(), inline=True)
    e.add_field(name='Kusů', value=str(quantity), inline=True)
    e.add_field(name='Důvod', value=reason.strip(), inline=False)
    e.add_field(name='Zapsal', value=interaction.user.mention, inline=True)
    e.add_field(name='Zbývá v GB', value=f'**{newq} ks**', inline=True)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name='stock', description='Nastaví aktuální počet itemu v guild bance')
@app_commands.describe(item='Název itemu', quantity='Aktuální počet kusů v GB')
async def stock(interaction: discord.Interaction, item: str, quantity: int):
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Inventář může měnit pouze důstojník / vedení.', ephemeral=True)
    if quantity < 0 or not item.strip() or len(item) > 200: return await interaction.response.send_message('❌ Zadej platný item a množství 0 nebo vyšší.', ephemeral=True)
    set_inventory(item, quantity, interaction.user.id)
    await interaction.response.send_message(f'📦 Inventář aktualizován: **{item.strip()} = {quantity} ks**.', ephemeral=True)

@bot.tree.command(name='inventory', description='Zobrazí inventář guild banky')
async def inventory(interaction: discord.Interaction):
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Inventář může zobrazit pouze důstojník / vedení.', ephemeral=True)
    c=db(); rows=c.execute('SELECT * FROM inventory ORDER BY item').fetchall(); c.close()
    if not rows: return await interaction.response.send_message('📦 Inventář je zatím prázdný.', ephemeral=True)
    desc='\n'.join(f'• **{r["item"]}** — {r["quantity"]} ks' for r in rows)
    await interaction.response.send_message(embed=discord.Embed(title='📦 INVENTÁŘ GUILD BANKY', description=desc, color=discord.Color.blurple()), ephemeral=True)

@bot.tree.command(name='inventory_check', description='Porovná očekávaný stav s fyzickým stavem v GB')
@app_commands.describe(item='Název itemu', actual_quantity='Kolik kusů skutečně vidíš v GB')
async def inventory_check(interaction: discord.Interaction, item: str, actual_quantity: int):
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Kontrolu inventáře může dělat pouze důstojník / vedení.', ephemeral=True)
    if actual_quantity < 0: return await interaction.response.send_message('❌ Počet nemůže být záporný.', ephemeral=True)
    inv=get_inventory(item)
    if not inv: return await interaction.response.send_message('❌ Item není v evidovaném inventáři. Nejprve použij `/stock`.', ephemeral=True)
    expected=inv['quantity']; diff=actual_quantity-expected
    if diff == 0: msg=f'✅ **Shoda.** Evidováno i skutečně nalezeno: **{actual_quantity} ks**.'
    elif diff < 0: msg=f'⚠️ **Manko {abs(diff)} ks.** Evidence říká **{expected} ks**, skutečně je **{actual_quantity} ks**. Bot sám nemůže určit, kdo item vzal.'
    else: msg=f'ℹ️ **Přebytek {diff} ks.** Evidence říká **{expected} ks**, skutečně je **{actual_quantity} ks**.'
    await interaction.response.send_message(embed=discord.Embed(title=f'🔎 KONTROLA: {item}', description=msg, color=discord.Color.orange() if diff else discord.Color.green()), ephemeral=True)

@bot.tree.command(name='items', description='Zobrazí poslední ruční zápisy guild itemů')
async def items(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento přehled může zobrazit pouze důstojník / vedení.', ephemeral=True)
    c = db(); rows = c.execute('SELECT * FROM items ORDER BY id DESC LIMIT 20').fetchall(); c.close()
    if not rows:
        return await interaction.response.send_message('📭 Zatím nejsou žádné ruční zápisy itemů.', ephemeral=True)
    lines=[]
    for r in rows:
        lines.append(f"**#{r['id']:03d}** • <@{r['player_id']}> • {r['item']} × {r['quantity']} • {r['reason']}")
    await interaction.response.send_message(embed=discord.Embed(title='📋 GB ITEM LOG', description='\n'.join(lines), color=discord.Color.blurple()), ephemeral=True)

@bot.tree.command(name='points', description='Ukáže tvůj týdenní stav bodů z orderů')
async def points(interaction: discord.Interaction):
    used = weekly_points(interaction.user.id)
    remaining = max(0, WEEKLY_POINT_LIMIT - used)
    await interaction.response.send_message(
        f'📊 Tento týden máš získáno nebo rezervováno **{used}/{WEEKLY_POINT_LIMIT} bodů**. '
        f'Do limitu zbývá **{remaining} bodů**.',
        ephemeral=True
    )

@bot.tree.command(name='order', description='Vytvoří novou guild objednávku')
@app_commands.describe(item='Název předmětu', quantity='Počet kusů', reward='Odměna v guild pointech')
async def order(interaction: discord.Interaction, item: str, quantity: int, reward: int):
    if quantity < 1 or reward < 0 or not item.strip() or len(item) > 200:
        return await interaction.response.send_message('❌ Zkontroluj množství, odměnu a název předmětu.', ephemeral=True)
    i = create_order(item.strip(), quantity, reward, interaction.user.id, interaction.channel_id)
    o = get_order(i)
    await interaction.response.send_message(embed=await embed_for(interaction.guild, o), view=OrderView(i))
    set_message(i, (await interaction.original_response()).id)

@bot.tree.command(name='orders', description='Zobrazí otevřené objednávky')
async def orders(interaction: discord.Interaction):
    c = db(); rows = c.execute("SELECT * FROM orders WHERE status IN ('OPEN','CLAIMED','PENDING_REVIEW') ORDER BY id LIMIT 20").fetchall(); c.close()
    if not rows:
        return await interaction.response.send_message('📭 Momentálně nejsou žádné otevřené objednávky.', ephemeral=True)
    lines = []
    for o in rows:
        who = f" — vyřizuje <@{o['claimer_id']}>" if o['claimer_id'] else ''
        lines.append(f"**#{o['id']:03d}** • {o['item']} × {o['quantity']} • **{o['reward']} bodů** • {status_text(o['status'])}{who}")
    await interaction.response.send_message(embed=discord.Embed(title='📋 Otevřené objednávky', description='\n'.join(lines), color=discord.Color.blurple()))

@bot.tree.command(name='order_info', description='Zobrazí detail objednávky')
@app_commands.describe(order_id='Číslo objednávky')
async def order_info(interaction: discord.Interaction, order_id: int):
    o = get_order(order_id)
    if not o:
        return await interaction.response.send_message('❌ Taková objednávka neexistuje.', ephemeral=True)
    await interaction.response.send_message(embed=await embed_for(interaction.guild, o), ephemeral=True)

bot.run(TOKEN)
