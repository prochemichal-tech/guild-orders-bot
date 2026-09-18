import os, sqlite3
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = os.getenv('DB_PATH', '/data/orders.db')
if not TOKEN:
    raise RuntimeError('Chybí proměnná DISCORD_TOKEN.')
os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL, quantity INTEGER NOT NULL,
        reward INTEGER NOT NULL, creator_id INTEGER NOT NULL, claimer_id INTEGER,
        status TEXT NOT NULL DEFAULT 'OPEN', channel_id INTEGER, message_id INTEGER,
        created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT, cancelled_at TEXT)'''); c.commit(); c.close()

def get_order(i):
    c=db(); r=c.execute('SELECT * FROM orders WHERE id=?',(i,)).fetchone(); c.close(); return r

def create_order(item,q,reward,uid,cid):
    c=db(); cur=c.execute('INSERT INTO orders(item,quantity,reward,creator_id,channel_id,created_at) VALUES(?,?,?,?,?,?)',
        (item,q,reward,uid,cid,datetime.now(timezone.utc).isoformat())); i=cur.lastrowid; c.commit(); c.close(); return i

def set_message(i,mid):
    c=db(); c.execute('UPDATE orders SET message_id=? WHERE id=?',(mid,i)); c.commit(); c.close()

def is_staff(m): return isinstance(m,discord.Member) and (m.guild_permissions.manage_guild or m.guild_permissions.administrator)

def status_text(s): return {'OPEN':'🟢 VOLNÉ','CLAIMED':'🟡 VYŘIZUJE','COMPLETED':'✅ SPLNĚNO','CANCELLED':'❌ ZRUŠENO'}.get(s,s)

async def uname(g,uid):
    m=g.get_member(uid)
    if m: return m.display_name
    try: return (await g.fetch_member(uid)).display_name
    except: return f'ID {uid}'

async def embed_for(g,o):
    col={'OPEN':discord.Color.gold(),'CLAIMED':discord.Color.orange(),'COMPLETED':discord.Color.green(),'CANCELLED':discord.Color.red()}[o['status']]
    e=discord.Embed(title=f"📦 ORDER #{o['id']:03d}",color=col)
    e.add_field(name='Předmět',value=o['item'],inline=False)
    e.add_field(name='Množství',value=str(o['quantity']),inline=True)
    e.add_field(name='Odměna',value=f"**{o['reward']} bodů**",inline=True)
    e.add_field(name='Stav',value=status_text(o['status']),inline=False)
    e.add_field(name='Zadal',value=await uname(g,o['creator_id']),inline=True)
    if o['claimer_id']: e.add_field(name='Vyřizuje',value=await uname(g,o['claimer_id']),inline=True)
    e.set_footer(text='Guild Orders • body se zatím připisují ručně')
    return e

def update_status(i,status,uid=None):
    c=db(); now=datetime.now(timezone.utc).isoformat()
    if status=='CLAIMED': cur=c.execute("UPDATE orders SET claimer_id=?,status='CLAIMED',claimed_at=? WHERE id=? AND status='OPEN'",(uid,now,i))
    elif status=='OPEN': cur=c.execute("UPDATE orders SET claimer_id=NULL,status='OPEN',claimed_at=NULL WHERE id=? AND status='CLAIMED'",(i,))
    elif status=='COMPLETED': cur=c.execute("UPDATE orders SET status='COMPLETED',completed_at=? WHERE id=? AND status='CLAIMED'",(now,i))
    else: cur=c.execute("UPDATE orders SET status='CANCELLED',cancelled_at=? WHERE id=? AND status NOT IN ('COMPLETED','CANCELLED')",(now,i))
    ok=cur.rowcount>0; c.commit(); c.close(); return ok

class OrderView(discord.ui.View):
    def __init__(self,i):
        super().__init__(timeout=None); self.i=i
        buttons=[('🙋','VZÍT ORDER',discord.ButtonStyle.success,'claim'),('↩️','UVOLNIT',discord.ButtonStyle.secondary,'release'),('✅','SPLNĚNO',discord.ButtonStyle.primary,'complete'),('❌','ZRUŠIT',discord.ButtonStyle.danger,'cancel')]
        for em,label,style,kind in buttons:
            b=discord.ui.Button(emoji=em,label=label,style=style,custom_id=f'guildorder:{kind}:{i}')
            b.callback=getattr(self,kind); self.add_item(b)
    async def redraw(self,interaction):
        o=get_order(self.i); await interaction.response.edit_message(embed=await embed_for(interaction.guild,o),view=OrderView(self.i))
    async def claim(self,interaction):
        if not update_status(self.i,'CLAIMED',interaction.user.id): return await interaction.response.send_message('❌ Objednávka už není volná.',ephemeral=True)
        await self.redraw(interaction)
    async def release(self,interaction):
        o=get_order(self.i)
        if not o or o['status']!='CLAIMED' or (o['claimer_id']!=interaction.user.id and not is_staff(interaction.user)):
            return await interaction.response.send_message('❌ Tuto objednávku může uvolnit pouze její řešitel nebo důstojník.',ephemeral=True)
        update_status(self.i,'OPEN'); await self.redraw(interaction)
    async def complete(self,interaction):
        if not is_staff(interaction.user): return await interaction.response.send_message('❌ SPLNĚNO může potvrdit pouze důstojník.',ephemeral=True)
        if not update_status(self.i,'COMPLETED'): return await interaction.response.send_message('❌ Objednávka není ve stavu VYŘIZUJE.',ephemeral=True)
        await self.redraw(interaction)
    async def cancel(self,interaction):
        o=get_order(self.i)
        if not o or (o['creator_id']!=interaction.user.id and not is_staff(interaction.user)):
            return await interaction.response.send_message('❌ Zrušit ji může zadavatel nebo důstojník.',ephemeral=True)
        if not update_status(self.i,'CANCELLED'): return await interaction.response.send_message('❌ Objednávku už nelze zrušit.',ephemeral=True)
        await self.redraw(interaction)

class Bot(commands.Bot):
    def __init__(self): super().__init__(command_prefix='!',intents=discord.Intents.default())
    async def setup_hook(self):
        init_db(); c=db(); rows=c.execute("SELECT id FROM orders WHERE status IN ('OPEN','CLAIMED')").fetchall(); c.close()
        for r in rows: self.add_view(OrderView(r['id']))
        await self.tree.sync()
    async def on_ready(self): print(f'Guild Orders online: {self.user} ({self.user.id})')

bot=Bot()

@bot.tree.command(name='order',description='Vytvoří novou guild objednávku')
@app_commands.describe(item='Název předmětu',quantity='Počet kusů',reward='Odměna v guild pointech')
async def order(interaction:discord.Interaction,item:str,quantity:int,reward:int):
    if quantity<1 or reward<0 or not item.strip() or len(item)>200:
        return await interaction.response.send_message('❌ Zkontroluj množství, odměnu a název předmětu.',ephemeral=True)
    i=create_order(item.strip(),quantity,reward,interaction.user.id,interaction.channel_id); o=get_order(i)
    await interaction.response.send_message(embed=await embed_for(interaction.guild,o),view=OrderView(i)); set_message(i,(await interaction.original_response()).id)

@bot.tree.command(name='orders',description='Zobrazí otevřené objednávky')
async def orders(interaction:discord.Interaction):
    c=db(); rows=c.execute("SELECT * FROM orders WHERE status IN ('OPEN','CLAIMED') ORDER BY id LIMIT 20").fetchall(); c.close()
    if not rows: return await interaction.response.send_message('📭 Momentálně nejsou žádné otevřené objednávky.',ephemeral=True)
    lines=[]
    for o in rows:
        who=f" — vyřizuje <@{o['claimer_id']}>" if o['claimer_id'] else ''
        lines.append(f"**#{o['id']:03d}** • {o['item']} × {o['quantity']} • **{o['reward']} bodů** • {status_text(o['status'])}{who}")
    await interaction.response.send_message(embed=discord.Embed(title='📋 Otevřené objednávky',description='\n'.join(lines),color=discord.Color.blurple()))

@bot.tree.command(name='order_info',description='Zobrazí detail objednávky')
@app_commands.describe(order_id='Číslo objednávky')
async def order_info(interaction:discord.Interaction,order_id:int):
    o=get_order(order_id)
    if not o: return await interaction.response.send_message('❌ Taková objednávka neexistuje.',ephemeral=True)
    await interaction.response.send_message(embed=await embed_for(interaction.guild,o),ephemeral=True)

bot.run(TOKEN)
