# Guild Orders V24 - per-player WoW character mapping + CLM award queue
import os, sqlite3
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = os.getenv('DB_PATH', '/data/orders.db')
WEEKLY_POINT_LIMIT = int(os.getenv('WEEKLY_POINT_LIMIT', '100'))
if not TOKEN:
    raise RuntimeError('Chybí proměnná DISCORD_TOKEN.')
os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS wow_characters (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        character_name TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS clm_awards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        character_name TEXT NOT NULL,
        ep INTEGER NOT NULL,
        order_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TEXT NOT NULL,
        processed_at TEXT
    )''')
    cols={r['name'] for r in c.execute("PRAGMA table_info(clm_awards)").fetchall()}
    if 'batch_id' not in cols:
        c.execute("ALTER TABLE clm_awards ADD COLUMN batch_id TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_clm_awards_batch ON clm_awards(guild_id,batch_id,status)")
    c.execute('''CREATE TABLE IF NOT EXISTS ep_resets (
        guild_id INTEGER PRIMARY KEY,
        reset_at TEXT NOT NULL,
        reset_by INTEGER NOT NULL
    )''')
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
    # Migrace starších databází.
    cols = {r['name'] for r in c.execute('PRAGMA table_info(orders)').fetchall()}
    if 'guild_id' not in cols:
        c.execute('ALTER TABLE orders ADD COLUMN guild_id INTEGER')
        cols.add('guild_id')
    if 'reusable' not in cols:
        c.execute('ALTER TABLE orders ADD COLUMN reusable INTEGER NOT NULL DEFAULT 0')
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
    # V11: vždy vytvořit pomocné tabulky i u starší existující databáze.
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            item TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_by INTEGER,
            updated_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventory_v14 (
        guild_id INTEGER NOT NULL, item TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0,
        updated_by INTEGER, updated_at TEXT, PRIMARY KEY (guild_id,item)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS items_v14 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL, item TEXT NOT NULL, quantity INTEGER NOT NULL,
        reason TEXT NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL
    )''')
    c.commit()
    c.close()

def set_wow_character(guild_id, user_id, character_name):
    name = ' '.join(character_name.strip().split())
    c = db()
    c.execute('''INSERT INTO wow_characters(guild_id,user_id,character_name,updated_at)
                 VALUES(?,?,?,?)
                 ON CONFLICT(guild_id,user_id) DO UPDATE SET
                 character_name=excluded.character_name, updated_at=excluded.updated_at''',
              (guild_id, user_id, name, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()
    return name

def get_wow_character(guild_id, user_id):
    c=db()
    r=c.execute('SELECT character_name FROM wow_characters WHERE guild_id=? AND user_id=?',
                (guild_id,user_id)).fetchone()
    c.close()
    return r['character_name'] if r else None

def queue_clm_award(guild_id, user_id, character_name, ep, order_id):
    c=db()
    now=datetime.now(timezone.utc).isoformat()
    c.execute('''INSERT INTO clm_awards
                 (guild_id,user_id,character_name,ep,order_id,status,created_at)
                 VALUES(?,?,?,?,?,'PENDING',?)
                 ON CONFLICT(order_id) DO UPDATE SET
                    guild_id=excluded.guild_id,
                    user_id=excluded.user_id,
                    character_name=excluded.character_name,
                    ep=excluded.ep,
                    status='PENDING',
                    created_at=excluded.created_at,
                    processed_at=NULL''',
              (guild_id,user_id,character_name,int(ep),order_id,now))
    c.commit(); c.close()

def get_order(i):
    c = db(); r = c.execute('SELECT * FROM orders WHERE id=?', (i,)).fetchone(); c.close(); return r


def _week_start_utc():
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def weekly_points(user_id, guild_id):
    """EP získané + rezervované v aktuálním týdnu (pondělí–neděle)."""
    start = _week_start_utc().isoformat()
    c = db()
    reset = c.execute('SELECT reset_at FROM ep_resets WHERE guild_id=?', (guild_id,)).fetchone()
    if reset and reset['reset_at'] > start:
        start = reset['reset_at']
    r = c.execute("""
        SELECT COALESCE(SUM(reward),0) AS total FROM orders
        WHERE claimer_id=? AND guild_id=?
          AND status IN ('CLAIMED','PENDING_REVIEW','COMPLETED')
          AND (
            (status='COMPLETED' AND reviewed_at>=?)
            OR
            (status!='COMPLETED' AND claimed_at>=?)
          )
    """,(user_id,guild_id,start,start)).fetchone()
    c.close()
    return int(r['total'] or 0)

def create_order(item, q, reward, uid, cid, guild_id):
    c = db()
    cur = c.execute(
        'INSERT INTO orders(item,quantity,reward,creator_id,channel_id,created_at,guild_id) VALUES(?,?,?,?,?,?,?)',
        (item, q, reward, uid, cid, datetime.now(timezone.utc).isoformat(), guild_id)
    )
    i = cur.lastrowid
    c.commit(); c.close(); return i

def set_message(i, mid):
    c = db(); c.execute('UPDATE orders SET message_id=? WHERE id=?', (mid, i)); c.commit(); c.close()

def is_staff(m):
    return isinstance(m, discord.Member) and (m.guild_permissions.manage_guild or m.guild_permissions.administrator)

# V22: Bot se používá jen v těchto dvou textových kanálech.
# Discord názvy kanálů jsou obvykle malými písmeny a mezery převádí na pomlčky.
ALLOWED_CHANNELS = {'guild-orders', 'guild-orders-gold'}

def is_allowed_channel(interaction):
    channel = getattr(interaction, 'channel', None)
    name = getattr(channel, 'name', '') or ''
    return name.casefold() in ALLOWED_CHANNELS

async def require_allowed_channel(interaction):
    if is_allowed_channel(interaction):
        return True
    message = '❌ Guild Orders lze používat pouze v kanálech **#guild-orders** a **#guild-orders-gold**.'
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False

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
    e.add_field(name='Odměna', value=f"**{o['reward']} EP**", inline=True)
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
    e.set_footer(text='Guild Orders • EP čekají na synchronizaci s CLM')
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
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_allowed_channel(interaction)

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
            await interaction.response.edit_message(
                content=f'✅ Order označen jako splněný. Guild slot: **#{slot}**. Čeká na kontrolu důstojníkem / vedením.',
                view=None
            )

            # V15: hlavní zprávu orderu aktualizujeme přes uložený channel_id + message_id.
            # Ephemeral výběr slotu nemusí mít spolehlivě stejný channel objekt.
            try:
                channel = interaction.guild.get_channel(o['channel_id'])
                if channel is None:
                    channel = await interaction.guild.fetch_channel(o['channel_id'])
                msg = await channel.fetch_message(o['message_id'])
                fresh = get_order(self.order_id)
                await msg.edit(
                    embed=await embed_for(interaction.guild, fresh),
                    view=OrderView(self.order_id)
                )
            except Exception as e:
                print(f'Order #{self.order_id}: nepodařilo se překreslit hlavní zprávu: {type(e).__name__}: {e}')
            self.stop()
        select.callback = callback
        self.add_item(select)

class ControlSlotView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_allowed_channel(interaction)

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

            # V24: každý hráč může mít vlastní Discord -> WoW postavu.
            # Po schválení připravíme jeho konkrétní EP odměnu pro pozdější CLM bridge.
            wow_character = get_wow_character(interaction.guild_id, o['claimer_id'])
            if wow_character:
                queue_clm_award(interaction.guild_id, o['claimer_id'], wow_character, o['reward'], o['id'])

            # Schválený order = dodané kusy se automaticky přičtou do inventáře guild banky.
            inv = get_inventory(interaction.guild_id, o['item'])
            if inv:
                new_inventory_quantity = change_inventory(interaction.guild_id, o['item'], o['quantity'], interaction.user.id)
            else:
                set_inventory(interaction.guild_id, o['item'], o['quantity'], interaction.user.id)
                new_inventory_quantity = o['quantity']

            await interaction.response.edit_message(
                content=(
                    f'✅ Order potvrzen. Kontrolní slot: **#{slot}**. '
                    f'Do inventáře bylo přidáno **{o["quantity"]}× {o["item"]}** '
                    f'(nový stav: **{new_inventory_quantity} ks**). EP odměna byla připravena pro CLM Bridge.' if wow_character else 'Hráč zatím nemá nastavenou WoW postavu přes /wow_character.'
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
                dm.add_field(name='Odměna', value=f'**{o["reward"]} EP**', inline=True)
                dm.add_field(
                    name='Informace',
                    value=(f'EP odměna byla připravena pro WoW postavu **{wow_character}**.'
                           if wow_character else
                           'EP zatím nebylo připraveno pro CLM. Nejdřív použij /wow_character.'),
                    inline=False
                )
                used_now = weekly_points(o['claimer_id'], interaction.guild_id)
                remaining_now = max(0, WEEKLY_POINT_LIMIT - used_now)
                dm.add_field(
                    name='📊 Týdenní EP',
                    value=f'**{used_now}/{WEEKLY_POINT_LIMIT} EP**\nZbývá ti ještě **{remaining_now} EP**.',
                    inline=False
                )
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
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_allowed_channel(interaction)

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

        used = weekly_points(interaction.user.id, interaction.guild_id)
        if used + o['reward'] > WEEKLY_POINT_LIMIT:
            return await interaction.response.send_message(
                f'❌ Tento order nemůžeš vzít. Tento týden máš získáno nebo rezervováno '
                f'**{used}/{WEEKLY_POINT_LIMIT} EP** a tento order je za **{o["reward"]} EP**. '
                f'Překročil bys týdenní limit.',
                ephemeral=True
            )

        if not update_status(self.i, 'CLAIMED', interaction.user.id):
            return await interaction.response.send_message('❌ Objednávka už není volná.', ephemeral=True)
        await self.redraw(interaction)
        used_now = weekly_points(interaction.user.id, interaction.guild_id)
        remaining_now = max(0, WEEKLY_POINT_LIMIT - used_now)
        try:
            await interaction.followup.send(
                f'📊 Týdenní stav: **{used_now}/{WEEKLY_POINT_LIMIT} EP**. '
                f'Zbývá ti ještě **{remaining_now} EP**.',
                ephemeral=True
            )
        except Exception:
            pass

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
            return await interaction.response.send_message('❌ Potvrdit splnění a udělit EP může pouze důstojník / vedení.', ephemeral=True)
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

def get_inventory(guild_id, item):
    c=db(); r=c.execute('SELECT * FROM inventory_v14 WHERE guild_id=? AND item=?',(guild_id,normalize_item(item))).fetchone(); c.close(); return r

def set_inventory(guild_id, item, quantity, uid):
    key=normalize_item(item); now=datetime.now(timezone.utc).isoformat(); c=db()
    c.execute('INSERT INTO inventory_v14(guild_id,item,quantity,updated_by,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,item) DO UPDATE SET quantity=excluded.quantity,updated_by=excluded.updated_by,updated_at=excluded.updated_at',(guild_id,key,quantity,uid,now))
    c.commit(); c.close()

def change_inventory(guild_id, item, delta, uid):
    key=normalize_item(item); now=datetime.now(timezone.utc).isoformat(); c=db()
    r=c.execute('SELECT quantity FROM inventory_v14 WHERE guild_id=? AND item=?',(guild_id,key)).fetchone()
    if not r: c.close(); return None
    newq=r['quantity']+delta
    if newq<0: c.close(); return None
    c.execute('UPDATE inventory_v14 SET quantity=?,updated_by=?,updated_at=? WHERE guild_id=? AND item=?',(newq,uid,now,guild_id,key))
    c.commit(); c.close(); return newq

def create_item_log(guild_id, player_id, item, quantity, reason, created_by):
    c=db(); cur=c.execute('INSERT INTO items_v14(guild_id,player_id,item,quantity,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)',
        (guild_id,player_id,item.strip(),quantity,reason.strip(),created_by,datetime.now(timezone.utc).isoformat()))
    n=cur.lastrowid; c.commit(); c.close(); return n

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        # Načíst samostatný modul pro potvrzení pravidel.
        await self.load_extension('rules_verify')
        print('Modul potvrzení pravidel načten.')

        # Připrav DB při každém startu.
        init_db()
        print(f'Databáze připravena: {DB_PATH}')

        # Globální synchronizace = bot funguje na všech serverech, kam je pozván.
        synced = await self.tree.sync()
        print(f'Globálně synchronizováno {len(synced)} příkazů.')

        # Na serverech, kde už bot je, uděláme navíc guild sync pro rychlé testování.
        # Příkazy zkopírujeme z globálního stromu.
        for guild in self.guilds:
            try:
                obj = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=obj)
                local = await self.tree.sync(guild=obj)
                print(f'Server {guild.id}: synchronizováno {len(local)} příkazů.')
            except Exception as e:
                print(f'Server {guild.id}: lokální sync selhal: {e}')

    async def on_ready(self):
        print(f'Guild Orders online: {self.user} ({self.user.id})')

    async def on_guild_join(self, guild):
        # Nový server: zpřístupnit slash příkazy hned po přidání bota.
        try:
            obj = discord.Object(id=guild.id)
            self.tree.copy_global_to(guild=obj)
            local = await self.tree.sync(guild=obj)
            print(f'Nový server {guild.id}: synchronizováno {len(local)} příkazů.')
        except Exception as e:
            print(f'Nový server {guild.id}: sync selhal: {e}')

bot = Bot()

@bot.tree.command(name='item', description='Ruční zápis vydaného guild itemu hráči')
@app_commands.describe(player='Hráč, který item dostal', item='Název itemu', quantity='Počet kusů', reason='Proč hráč item dostal')
async def item(interaction: discord.Interaction, player: discord.Member, item: str, quantity: int, reason: str):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento zápis může dělat pouze důstojník / vedení.', ephemeral=True)
    if quantity < 1 or not item.strip() or len(item) > 200 or not reason.strip() or len(reason) > 500:
        return await interaction.response.send_message('❌ Zkontroluj item, počet kusů a důvod.', ephemeral=True)
    inv = get_inventory(interaction.guild_id, item)
    if not inv:
        return await interaction.response.send_message('❌ Tento item zatím není v inventáři. Nejprve ho přidej přes `/stock` s počátečním množstvím.', ephemeral=True)
    if inv['quantity'] < quantity:
        return await interaction.response.send_message(f'❌ V inventáři je jen **{inv["quantity"]} ks** tohoto itemu, nelze vydat {quantity} ks.', ephemeral=True)
    newq = change_inventory(interaction.guild_id, item, -quantity, interaction.user.id)
    item_id = create_item_log(interaction.guild_id, player.id, item.strip(), quantity, reason.strip(), interaction.user.id)
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
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Inventář může měnit pouze důstojník / vedení.', ephemeral=True)
    if quantity < 0 or not item.strip() or len(item) > 200: return await interaction.response.send_message('❌ Zadej platný item a množství 0 nebo vyšší.', ephemeral=True)
    set_inventory(interaction.guild_id, item, quantity, interaction.user.id)
    await interaction.response.send_message(f'📦 Inventář aktualizován: **{item.strip()} = {quantity} ks**.', ephemeral=True)

@bot.tree.command(name='inventory', description='Zobrazí inventář guild banky')
async def inventory(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Inventář může zobrazit pouze důstojník / vedení.', ephemeral=True)
    c=db(); rows=c.execute('SELECT * FROM inventory_v14 WHERE guild_id=? ORDER BY item',(interaction.guild_id,)).fetchall(); c.close()
    if not rows: return await interaction.response.send_message('📦 Inventář je zatím prázdný.', ephemeral=True)
    desc='\n'.join(f'• **{r["item"]}** — {r["quantity"]} ks' for r in rows)
    await interaction.response.send_message(embed=discord.Embed(title='📦 INVENTÁŘ GUILD BANKY', description=desc, color=discord.Color.blurple()), ephemeral=True)

@bot.tree.command(name='inventory_check', description='Porovná očekávaný stav s fyzickým stavem v GB')
@app_commands.describe(item='Název itemu', actual_quantity='Kolik kusů skutečně vidíš v GB')
async def inventory_check(interaction: discord.Interaction, item: str, actual_quantity: int):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user): return await interaction.response.send_message('❌ Kontrolu inventáře může dělat pouze důstojník / vedení.', ephemeral=True)
    if actual_quantity < 0: return await interaction.response.send_message('❌ Počet nemůže být záporný.', ephemeral=True)
    inv=get_inventory(interaction.guild_id, item)
    if not inv: return await interaction.response.send_message('❌ Item není v evidovaném inventáři. Nejprve použij `/stock`.', ephemeral=True)
    expected=inv['quantity']; diff=actual_quantity-expected
    if diff == 0: msg=f'✅ **Shoda.** Evidováno i skutečně nalezeno: **{actual_quantity} ks**.'
    elif diff < 0: msg=f'⚠️ **Manko {abs(diff)} ks.** Evidence říká **{expected} ks**, skutečně je **{actual_quantity} ks**. Bot sám nemůže určit, kdo item vzal.'
    else: msg=f'ℹ️ **Přebytek {diff} ks.** Evidence říká **{expected} ks**, skutečně je **{actual_quantity} ks**.'
    await interaction.response.send_message(embed=discord.Embed(title=f'🔎 KONTROLA: {item}', description=msg, color=discord.Color.orange() if diff else discord.Color.green()), ephemeral=True)

@bot.tree.command(name='items', description='Zobrazí poslední ruční zápisy guild itemů')
async def items(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento přehled může zobrazit pouze důstojník / vedení.', ephemeral=True)
    c = db(); rows = c.execute('SELECT * FROM items_v14 WHERE guild_id=? ORDER BY id DESC LIMIT 20',(interaction.guild_id,)).fetchall(); c.close()
    if not rows:
        return await interaction.response.send_message('📭 Zatím nejsou žádné ruční zápisy itemů.', ephemeral=True)
    lines=[]
    for r in rows:
        lines.append(f"**#{r['id']:03d}** • <@{r['player_id']}> • {r['item']} × {r['quantity']} • {r['reason']}")
    await interaction.response.send_message(embed=discord.Embed(title='📋 GB ITEM LOG', description='\n'.join(lines), color=discord.Color.blurple()), ephemeral=True)

@bot.tree.command(name='ep_reset', description='Vedení: vynuluje EP limit všem hráčům')
async def ep_reset(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Pouze důstojník / vedení.', ephemeral=True)
    now=datetime.now(timezone.utc).isoformat()
    c=db()
    c.execute('INSERT INTO ep_resets(guild_id,reset_at,reset_by) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET reset_at=excluded.reset_at, reset_by=excluded.reset_by',
              (interaction.guild_id,now,interaction.user.id))
    c.commit(); c.close()
    await interaction.response.send_message(f'✅ EP byly resetovány. Nový limit je **{WEEKLY_POINT_LIMIT} EP**.', ephemeral=True)

@bot.tree.command(name='points_all', description='Vedení: ukáže týdenní EP všech hráčů')
async def points_all(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento přehled může zobrazit pouze důstojník / vedení.', ephemeral=True)
    start = _week_start_utc().isoformat()
    c = db()
    reset = c.execute('SELECT reset_at FROM ep_resets WHERE guild_id=?', (interaction.guild_id,)).fetchone()
    if reset and reset['reset_at'] > start:
        start = reset['reset_at']
    rows = c.execute("""SELECT claimer_id, COALESCE(SUM(reward),0) AS total FROM orders
        WHERE guild_id=? AND claimer_id IS NOT NULL
        AND status IN ('CLAIMED','PENDING_REVIEW','COMPLETED')
        AND ((status='COMPLETED' AND reviewed_at>=?) OR (status!='COMPLETED' AND claimed_at>=?))
        GROUP BY claimer_id ORDER BY total DESC""", (interaction.guild_id,start,start)).fetchall()
    c.close()
    if not rows:
        return await interaction.response.send_message(f'📊 Tento týden zatím nikdo nemá EP. Limit je **{WEEKLY_POINT_LIMIT}**.', ephemeral=True)
    lines=[]
    for r in rows:
        user_id = r['claimer_id']
        m = interaction.guild.get_member(user_id)
        if m is None:
            try:
                m = await interaction.guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                m = None

        if m is not None:
            name = m.display_name
            player_label = f'**{name}** ({m.mention})'
        else:
            # Fallback pro uživatele, který už na serveru není.
            try:
                u = await bot.fetch_user(user_id)
                player_label = f'**{u.name}**'
            except (discord.NotFound, discord.HTTPException):
                player_label = f'Neznámý / bývalý člen (`{user_id}`)'

        used=int(r['total'] or 0); remaining=max(0,WEEKLY_POINT_LIMIT-used)
        lines.append(f'{player_label} — **{used}/{WEEKLY_POINT_LIMIT} EP** • zbývá **{remaining} EP**')
    await interaction.response.send_message(embed=discord.Embed(title='📊 Týdenní EP hráčů',description='\n'.join(lines)[:4000],color=discord.Color.blurple()),ephemeral=True)

@bot.tree.command(name='wow_character', description='Propojí tvůj Discord účet s WoW postavou pro CLM EP')
@app_commands.describe(name='Jméno postavy včetně realmu, např. Sjxtest-Thunderstrike')
async def wow_character(interaction: discord.Interaction, name: str):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    name = ' '.join(name.strip().split())
    if len(name) < 3 or len(name) > 80:
        return await interaction.response.send_message('❌ Zadej platné jméno WoW postavy.', ephemeral=True)
    saved = set_wow_character(interaction.guild_id, interaction.user.id, name)
    await interaction.response.send_message(
        f'✅ Tvůj Discord účet je pro tento server propojen s WoW postavou **{saved}**. '
        'Toto nastavení platí jen pro tebe; každý hráč si nastaví vlastní postavu.',
        ephemeral=True
    )

@bot.tree.command(name='wow_character_show', description='Ukáže tvoji propojenou WoW postavu')
async def wow_character_show(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    name = get_wow_character(interaction.guild_id, interaction.user.id)
    if not name:
        return await interaction.response.send_message('ℹ️ Zatím nemáš propojenou WoW postavu. Použij /wow_character.', ephemeral=True)
    await interaction.response.send_message(f'🎮 Tvoje propojená WoW postava: **{name}**', ephemeral=True)

@bot.tree.command(name='clm_export', description='Vedení: export čekajících EP odměn pro WoW CLM Bridge')
async def clm_export(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento příkaz je pouze pro vedení / důstojníky.', ephemeral=True)
    c=db()
    rows=c.execute("SELECT a.id, a.character_name, o.reward AS ep, a.order_id FROM clm_awards a JOIN orders o ON o.id=a.order_id WHERE a.guild_id=? AND a.status='PENDING' AND a.batch_id IS NULL ORDER BY a.id ASC LIMIT 50", (interaction.guild_id,)).fetchall()
    if not rows:
        c.close()
        return await interaction.response.send_message('ℹ️ Nejsou žádné nové čekající CLM EP odměny.', ephemeral=True)
    existing=c.execute("SELECT batch_id FROM clm_awards WHERE guild_id=? AND batch_id IS NOT NULL ORDER BY id DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    seq=1
    if existing and existing['batch_id'] and existing['batch_id'].startswith('BATCH-'):
        try: seq=int(existing['batch_id'].split('-')[1])+1
        except: seq=1
    batch_id=f'BATCH-{seq:04d}'
    ids=[r['id'] for r in rows]
    marks=','.join('?' for _ in ids)
    c.execute(f"UPDATE clm_awards SET batch_id=? WHERE guild_id=? AND id IN ({marks})", (batch_id,interaction.guild_id,*ids))
    c.commit()
    payload='GOB1|'+';'.join(f"{r['character_name']},{int(r['ep'])},ORDER-{r['order_id']}" for r in rows)
    c.close()
    await interaction.response.send_message(f"📤 **CLM export – {len(rows)} odměn**\n**Dávka:** `{batch_id}`\nVe WoW použij `/gobimport` a za něj vlož tento kód:\n```{payload}```\nPo úspěšném importu potvrď tuto dávku přes `/clm_confirm_batch`.", ephemeral=True)

@bot.tree.command(name='clm_confirm_batch', description='Vedení: potvrdí konkrétní exportovanou CLM dávku')
@app_commands.describe(batch_id='ID dávky, např. BATCH-0001')
async def clm_confirm_batch(interaction: discord.Interaction, batch_id: str):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento příkaz je pouze pro vedení / důstojníky.', ephemeral=True)
    batch_id=batch_id.strip().upper()
    c=db()
    rows=c.execute("SELECT id FROM clm_awards WHERE guild_id=? AND batch_id=? AND status='PENDING'", (interaction.guild_id,batch_id)).fetchall()
    if not rows:
        c.close()
        return await interaction.response.send_message(f'ℹ️ Dávka `{batch_id}` nemá žádné čekající odměny.', ephemeral=True)
    now=datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE clm_awards SET status='IMPORTED', processed_at=? WHERE guild_id=? AND batch_id=? AND status='PENDING'", (now,interaction.guild_id,batch_id))
    c.commit(); count=len(rows); c.close()
    await interaction.response.send_message(f'✅ Dávka `{batch_id}` potvrzena: **{count}** odměn označeno jako IMPORTED.', ephemeral=True)

@bot.tree.command(name='clm_confirm_all', description='Vedení: informace k bezpečnému potvrzení CLM dávky')
async def clm_confirm_all(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento příkaz je pouze pro vedení / důstojníky.', ephemeral=True)
    await interaction.response.send_message('🛡️ V28 používá BATCH-ID. Po `/clm_export` potvrď konkrétní dávku přes `/clm_confirm_batch`.', ephemeral=True)

@bot.tree.command(name='clm_confirm', description='Vedení: označí ORDER-ID jako úspěšně importované do CLM')
@app_commands.describe(order_id='Číslo orderu, např. 2 pro ORDER-2')
async def clm_confirm(interaction: discord.Interaction, order_id: int):
    if not interaction.guild_id:
        return await interaction.response.send_message('❌ Tento příkaz použij na Discord serveru.', ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Tento příkaz je pouze pro vedení / důstojníky.', ephemeral=True)
    c=db()
    row=c.execute("SELECT id,status FROM clm_awards WHERE guild_id=? AND order_id=?", (interaction.guild_id,order_id)).fetchone()
    if not row:
        c.close()
        return await interaction.response.send_message(f'❌ ORDER-{order_id} není v CLM frontě.', ephemeral=True)
    if row['status']=='IMPORTED':
        c.close()
        return await interaction.response.send_message(f'ℹ️ ORDER-{order_id} už je označen jako IMPORTED.', ephemeral=True)
    c.execute("UPDATE clm_awards SET status='IMPORTED', processed_at=? WHERE guild_id=? AND order_id=?",
              (datetime.now(timezone.utc).isoformat(),interaction.guild_id,order_id))
    c.commit(); c.close()
    await interaction.response.send_message(f'✅ ORDER-{order_id} označen jako IMPORTED. `/clm_export` ho už nenabídne.', ephemeral=True)

@bot.tree.command(name='points', description='Ukáže tvůj týdenní stav EP z orderů')
async def points(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    used = weekly_points(interaction.user.id, interaction.guild_id)
    remaining = max(0, WEEKLY_POINT_LIMIT - used)
    await interaction.response.send_message(
        f'📊 Tento týden máš získáno nebo rezervováno **{used}/{WEEKLY_POINT_LIMIT} EP**. '
        f'Do limitu zbývá **{remaining} EP**.',
        ephemeral=True
    )

@bot.tree.command(name='order_multi', description='Vedení: vytvoří opakovatelný order pro více hráčů')
@app_commands.describe(item='Předmět', quantity='Množství na jedno splnění', reward='EP za jedno splnění')
async def order_multi(interaction: discord.Interaction, item: str, quantity: int, reward: int):
    if not await require_allowed_channel(interaction):
        return
    if not is_staff(interaction.user):
        return await interaction.response.send_message('❌ Order může vytvořit pouze důstojník / vedení.', ephemeral=True)
    if quantity < 1 or reward < 1:
        return await interaction.response.send_message('❌ Množství i EP musí být alespoň 1.', ephemeral=True)
    if reward > WEEKLY_POINT_LIMIT:
        return await interaction.response.send_message(f'❌ Jedno splnění nemůže být za více než {WEEKLY_POINT_LIMIT} EP.', ephemeral=True)

    creator_id = interaction.user.id

    class MultiOrderView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='🙋 VZÍT ORDER', style=discord.ButtonStyle.success)
        async def fulfill(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if not await require_allowed_channel(button_interaction):
                return
            used = weekly_points(button_interaction.user.id, button_interaction.guild_id)
            if used + reward > WEEKLY_POINT_LIMIT:
                return await button_interaction.response.send_message(
                    f'❌ Tímto orderem bys překročil týdenní limit {WEEKLY_POINT_LIMIT} EP. '
                    f'Máš {used}/{WEEKLY_POINT_LIMIT} EP.', ephemeral=True)

            # Každé převzetí společného orderu vytvoří hráči normální vlastní order.
            # Díky tomu má stejné ovládání jako /order: UVOLNIT -> POSLAT KE KONTROLE -> schválení.
            oid = create_order(item, quantity, reward, creator_id, button_interaction.channel_id, button_interaction.guild_id)
            c=db()
            now=datetime.now(timezone.utc).isoformat()
            c.execute("UPDATE orders SET claimer_id=?, status='CLAIMED', claimed_at=? WHERE id=?",
                      (button_interaction.user.id, now, oid))
            c.commit(); c.close()
            child = get_order(oid)

            try:
                msg = await button_interaction.channel.send(
                    content=f'👤 {button_interaction.user.mention} převzal společný order:',
                    embed=await embed_for(button_interaction.guild, child),
                    view=OrderView(oid))
                set_message(oid, msg.id)
            except discord.HTTPException as e:
                # Když kartu nejde poslat, nenecháme hráči viset rezervované EP.
                update_status(oid, 'CANCELLED')
                return await button_interaction.response.send_message(
                    '❌ Nepodařilo se vytvořit kartu orderu. Zkontroluj oprávnění bota v tomto kanálu.',
                    ephemeral=True)

            await button_interaction.response.send_message(
                f'✅ Vzal sis **{item} ×{quantity}** za **{reward} EP**. '
                f'Pod společným orderem se vytvořila tvoje karta **ORDER #{oid:03d}**. '
                'Na ní můžeš order **UVOLNIT** nebo **OZNAČIT JAKO SPLNĚNÉ** a poslat ho dál ke kontrole.',
                ephemeral=True)

        @discord.ui.button(label='❌ ZRUŠIT SPOLEČNÝ ORDER', style=discord.ButtonStyle.danger)
        async def cancel_multi(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if not await require_allowed_channel(button_interaction):
                return
            if button_interaction.user.id != creator_id and not is_staff(button_interaction.user):
                return await button_interaction.response.send_message(
                    '❌ Společný order může zrušit pouze zadavatel nebo důstojník / vedení.', ephemeral=True)
            e = discord.Embed(
                title='❌ SPOLEČNÝ ORDER ZRUŠEN',
                description='Tento společný order už nelze dále přebírat.',
                color=discord.Color.red())
            e.add_field(name='Předmět', value=item, inline=False)
            e.add_field(name='Množství / jedno splnění', value=str(quantity), inline=True)
            e.add_field(name='Odměna / jedno splnění', value=f'{reward} EP', inline=True)
            await button_interaction.response.edit_message(embed=e, view=None)
            self.stop()

    e=discord.Embed(
        title='♻️ SPOLEČNÝ ORDER',
        description=(
            'Tento order může postupně převzít více hráčů. Každému hráči se po převzetí vytvoří '
            'vlastní normální karta orderu, kterou může uvolnit nebo poslat ke kontrole. '
            'Společný order zůstává aktivní, dokud ho vedení nezruší.'),
        color=discord.Color.green())
    e.add_field(name='Předmět', value=item, inline=False)
    e.add_field(name='Množství / jedno splnění', value=str(quantity), inline=True)
    e.add_field(name='Odměna / jedno splnění', value=f'{reward} EP', inline=True)
    e.add_field(name='Týdenní limit', value=f'{WEEKLY_POINT_LIMIT} EP / hráč', inline=False)
    await interaction.response.send_message(embed=e, view=MultiOrderView())

@bot.tree.command(name='order', description='Vytvoří novou guild objednávku')
@app_commands.describe(item='Název předmětu', quantity='Počet kusů', reward='Odměna v EP')
async def order(interaction: discord.Interaction, item: str, quantity: int, reward: int):
    if not await require_allowed_channel(interaction):
        return
    if quantity < 1 or reward < 0 or not item.strip() or len(item) > 200:
        return await interaction.response.send_message('❌ Zkontroluj množství, odměnu a název předmětu.', ephemeral=True)
    i = create_order(item.strip(), quantity, reward, interaction.user.id, interaction.channel_id, interaction.guild_id)
    o = get_order(i)
    await interaction.response.send_message(embed=await embed_for(interaction.guild, o), view=OrderView(i))
    set_message(i, (await interaction.original_response()).id)

@bot.tree.command(name='orders', description='Zobrazí otevřené objednávky')
async def orders(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    c = db(); rows = c.execute("SELECT * FROM orders WHERE status IN ('OPEN','CLAIMED','PENDING_REVIEW') ORDER BY id LIMIT 20").fetchall(); c.close()
    if not rows:
        return await interaction.response.send_message('📭 Momentálně nejsou žádné otevřené objednávky.', ephemeral=True)
    lines = []
    for o in rows:
        who = f" — vyřizuje <@{o['claimer_id']}>" if o['claimer_id'] else ''
        lines.append(f"**#{o['id']:03d}** • {o['item']} × {o['quantity']} • **{o['reward']} EP** • {status_text(o['status'])}{who}")
    await interaction.response.send_message(embed=discord.Embed(title='📋 Otevřené objednávky', description='\n'.join(lines), color=discord.Color.blurple()))

@bot.tree.command(name='order_info', description='Zobrazí detail objednávky')
@app_commands.describe(order_id='Číslo objednávky')
async def order_info(interaction: discord.Interaction, order_id: int):
    if not await require_allowed_channel(interaction):
        return
    o = get_order(order_id)
    if not o:
        return await interaction.response.send_message('❌ Taková objednávka neexistuje.', ephemeral=True)
    await interaction.response.send_message(embed=await embed_for(interaction.guild, o), ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f'Chyba slash příkazu: {type(error).__name__}: {error}')
    try:
        if interaction.response.is_done():
            await interaction.followup.send('❌ Při zpracování příkazu nastala chyba. Vedení může zkontrolovat Railway log.', ephemeral=True)
        else:
            await interaction.response.send_message('❌ Při zpracování příkazu nastala chyba. Vedení může zkontrolovat Railway log.', ephemeral=True)
    except Exception:
        pass

@bot.event
async def on_ready():
    print(f'Guild Orders online: {bot.user} ({bot.user.id})')
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced_local = await bot.tree.sync(guild=guild)
            print(f'Server {guild.name} ({guild.id}): synchronizováno {len(synced_local)} příkazů.')
        except Exception as e:
            print(f'Server {guild.id}: chyba synchronizace příkazů: {type(e).__name__}: {e}')

bot.run(TOKEN)
