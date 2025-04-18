import discord
from discord.ext import commands
import json, os, csv, aiohttp, urllib.parse, shlex, re

# Embed style and bot metadata
EMBED_COLOR = discord.Color.blurple()
BOT_NAME = "MTG League Bot"

# Globals for current league
# data_file: path to JSON storing league state
# card_csv: path to CSV storing card additions
# data: in-memory league data
# pending_scores: tracks active scoring messages

data_file = None
card_csv = None
data = {}
pending_scores = {}

token = os.getenv('DISCORD_TOKEN')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- Helper functions ---

def save_data():
    with open(data_file, 'w') as f:
        json.dump(data, f, indent=2)

# Build a consistent embed with author, timestamp, footer

def make_embed(title: str = None, description: str = '') -> discord.Embed:
    embed = discord.Embed(title=title, description=description,
                          color=EMBED_COLOR,
                          timestamp=discord.utils.utcnow())
    embed.set_author(name=BOT_NAME)
    embed.set_footer(text=f"League: {data.get('league_name', 'n/a')}")
    return embed

# Remove old bot messages (but keep week summaries and leaderboards)

enabled_delete = lambda t: not (t and (t.startswith('Week ') or 'Finalized' in t or 'Leaderboard' in t))

async def clean_send(channel, *, title=None, description=''):
    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.embeds:
            emb = msg.embeds[0]
            t = emb.title if emb.title else None
            if enabled_delete(t):
                await msg.delete()
                break
    embed = make_embed(title, description)
    return await channel.send(embed=embed)

# Auto-finalize a week: compute scores, allowances, and post summary

async def finalize_week_procedures(channel, week: str):
    wk_data = data['weeks'][week]

    # 1) final_scores
    final_scores = {}
    for game in wk_data['games'].values():
        for pid, rec in game.items():
            final_scores[pid] = final_scores.get(pid, 0) + rec['points']
    wk_data['final_scores'] = final_scores

    # 2) allowances based on average placement
    num_games = wk_data.get('num_games', len(wk_data['games']))
    players = data['players']
    caps = {'win': (1, 5), 'middle': (3, 10), 'last': (5, 15)}
    allowances = {}
    for pid in players:
        pid_str = str(pid)
        total_place = sum(g[pid_str]['placement'] for g in wk_data['games'].values())
        avg_place = total_place / num_games
        if avg_place == 1:
            cat = 'win'
        elif avg_place == len(players):
            cat = 'last'
        else:
            cat = 'middle'
        card_lim, price_lim = caps[cat]
        allowances[pid_str] = {
            'category': cat,
            'card_limit': card_lim,
            'price_limit': price_lim
        }
    wk_data['allowances'] = allowances

    # 3) collect card additions for the week
    cards = {}
    if os.path.exists(card_csv):
        with open(card_csv) as f:
            for row in csv.DictReader(f):
                if row['week'] == week:
                    cards.setdefault(row['user_id'], []).append(
                        f"{row['card_name']} (${row['price']})"
                    )
    wk_data['card_additions'] = cards

    # 4) mark finalized and save
    wk_data['finalized'] = True
    save_data()

    # 5) post summary embed
    embed = make_embed(f"Week {week} Finalized")
    # Final Scores
    score_lines = "\n".join(
        f"🏅 {channel.guild.get_member(int(pid)).display_name}: **{pts} pts**"
        for pid, pts in sorted(final_scores.items(), key=lambda x: -x[1])
    ) or "No scores."
    embed.add_field(name="Final Scores", value=score_lines, inline=False)

    # Allowances
    allow_lines = "\n".join(
        f"💳 {channel.guild.get_member(int(pid)).display_name}: {info['category'].title()} — {info['card_limit']} cards / ${info['price_limit']}"
        for pid, info in allowances.items()
    ) or "No allowances."
    embed.add_field(name="Allowances", value=allow_lines, inline=False)

    # Card additions
    card_lines = "\n".join(
        f"📦 {channel.guild.get_member(int(uid)).display_name}: {', '.join(lst)}"
        for uid, lst in cards.items()
    ) or "No cards added."
    embed.add_field(name="Cards Added", value=card_lines, inline=False)

    await channel.send(embed=embed)

# --- Scryfall API helpers ---

async def scryfall_search(query: str):
    url = f'https://api.scryfall.com/cards/search?format=json&q={urllib.parse.quote(query)}'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

async def scryfall_get_tcg(card_id: int):
    url = f'https://api.scryfall.com/cards/tcgplayer/{card_id}'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

# --- Bot commands ---

@bot.command(name='commands')
async def list_commands(ctx):
    cmds = [
        '!createleague "League Name" @p1 @p2 ...',
        '!loadleague "League Name"',
        '!addscores [num_games]',
        '!viewscores',
        '!editscores <week> <game> <@user> <placement>',
        '!addcard',
        '!removecard',
        '!viewcards <@user>',
        '!finalizeweek',
        '!commands'
    ]
    await clean_send(ctx.channel, title='Available Commands',
                     description="\n".join(cmds))

@bot.command(name='createleague')
async def create_league(ctx, *, args: str):
    tokens = shlex.split(args)
    if len(tokens) < 2:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='Usage: !createleague "League Name" @p1 @p2 ...')
    league_name = tokens[0]
    mentions = tokens[1:]
    members = []
    for m in mentions:
        match = re.match(r'<@!?(\d+)>', m)
        if match:
            member = ctx.guild.get_member(int(match.group(1)))
            if member:
                members.append(member)
    if not members:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No valid members mentioned.')

    guild_id = str(ctx.guild.id)
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', league_name)
    global data_file, card_csv, data
    data_file = f'league_{guild_id}_{safe}.json'
    card_csv = f'cards_{guild_id}_{safe}.csv'
    data = {
        'league_name': league_name,
        'players': [m.id for m in members],
        'weeks': {}
    }
    with open(data_file, 'w') as f:
        json.dump(data, f, indent=2)
    if not os.path.exists(card_csv):
        with open(card_csv, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['week','user_id','card_name','tcgplayer_id','price'])

    desc = f"**League:** {league_name}\n" + \
           f"**Players:** {' ,'.join(m.mention for m in members)}"
    await clean_send(ctx.channel, title='League Created', description=desc)

@bot.command(name='loadleague')
async def load_league(ctx, *, args: str):
    tokens = shlex.split(args)
    if len(tokens) != 1:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='Usage: !loadleague "League Name"')
    league_name = tokens[0]
    guild_id = str(ctx.guild.id)
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', league_name)
    df = f'league_{guild_id}_{safe}.json'
    cc = f'cards_{guild_id}_{safe}.csv'
    if not os.path.exists(df):
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description=f'League "{league_name}" not found.')
    global data_file, card_csv, data
    data_file = df
    card_csv = cc
    with open(data_file) as f:
        data = json.load(f)
    await clean_send(ctx.channel, title='League Loaded', description=f"Loaded **{data['league_name']}**")

@bot.command(name='addscores')
async def add_scores(ctx, num_games: int = 1):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    # find or create current week
    current = next((w for w,wk in data['weeks'].items() if not wk.get('finalized')), None)
    if current is None:
        current = str(len(data['weeks']) + 1)
        data['weeks'][current] = {
            'games': {},
            'finalized': False,
            'num_games': num_games
        }
        save_data()
    players = data['players']
    for g in range(1, num_games+1):
        choices = "\n".join(
            f":{i}: {ctx.guild.get_member(pid).mention}"
            for i,pid in enumerate(players,1)
        )
        embed = make_embed(f"Week {current} - Game {g}",
                           f"React with placement:\n{choices}")
        msg = await ctx.send(embed=embed)
        for i in range(1, len(players)+1):
            await msg.add_reaction(f"{i}⃣")
        pending_scores[msg.id] = {
            'week': current,
            'game': str(g),
            'players': set(str(pid) for pid in players),
            'responses': {}
        }

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or data_file is None:
        return
    mid = reaction.message.id
    if mid not in pending_scores:
        return
    info = pending_scores[mid]
    week, game = info['week'], info['game']
    if data['weeks'][week]['finalized']:
        return
    try:
        place = int(reaction.emoji[0])
    except:
        return
    pid = str(user.id)
    total = len(info['players'])
    points = 3 if place == 1 else (0 if place == total else 1)
    info['responses'][pid] = {'placement': place, 'points': points}

    if info['players'] == set(info['responses'].keys()):
        # all have reacted
        data['weeks'][week]['games'][game] = info['responses']
        save_data()
        # show results embed
        lines = "\n".join(
            f"{reaction.message.guild.get_member(int(p)).display_name}: place {r['placement']} — **{r['points']} pts**"
            for p,r in sorted(info['responses'].items(), key=lambda x: x[1]['placement'])
        )
        await clean_send(reaction.message.channel,
                         title=f"Results W{week} G{game}",
                         description=lines)
        del pending_scores[mid]
        # if this was the last game, finalize week
        wk = data['weeks'][week]
        if not wk.get('finalized') and len(wk['games']) == wk.get('num_games', 0):
            await finalize_week_procedures(reaction.message.channel, week)

@bot.command(name='viewscores')
async def view_scores(ctx):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    totals = {}
    for wk in data['weeks'].values():
        for gm in wk['games'].values():
            for pid, rec in gm.items():
                totals[pid] = totals.get(pid, 0) + rec['points']
    if not totals:
        return await clean_send(ctx.channel,
                                 title='No Data',
                                 description='No scores recorded yet.')
    lines = "\n".join(
        f"{ctx.guild.get_member(int(pid)).display_name}: **{pts} pts**"
        for pid, pts in sorted(totals.items(), key=lambda x: -x[1])
    )
    await clean_send(ctx.channel, title='🏆 Leaderboard', description=lines)

@bot.command(name='editscores')
async def edit_scores(ctx, week: int, game: int, member: discord.Member, placement: int):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    w, g = str(week), str(game)
    wk = data['weeks'].get(w)
    if wk and wk['games'].get(g):
        total = len(data['players'])
        pts = 3 if placement == 1 else (0 if placement == total else 1)
        wk['games'][g][str(member.id)] = {'placement': placement, 'points': pts}
        save_data()
        await clean_send(ctx.channel,
                         title='Score Updated',
                         description=f"W{w} G{g} {member.display_name} -> place {placement}")
    else:
        await clean_send(ctx.channel,
                         title='Error',
                         description='Invalid week/game.')

@bot.command(name='addcard')
async def add_card(ctx):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    # Ensure scores are finalized
    wk = str(len(data['weeks']))
    wk_data = data['weeks'][wk]
    if not wk_data.get('finalized'):
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='Week not finalized yet.')

    pid = str(ctx.author.id)
    # Calculate cumulative allowance across all finalized weeks
    total_allowed_cards = 0
    total_allowed_price = 0.0
    for w, wdata in data['weeks'].items():
        if wdata.get('finalized'):
            info = wdata['allowances'].get(pid, {})
            total_allowed_cards += info.get('card_limit', 0)
            total_allowed_price += info.get('price_limit', 0)
    # Calculate total used across season
    all_entries = [r for r in csv.DictReader(open(card_csv)) if r['user_id'] == pid]
    total_used_cards = len(all_entries)
    total_used_price = sum(float(r['price']) for r in all_entries)

    # Prompt user with cumulative allowance
    await clean_send(ctx.channel, title='Add Card',
                     description=(
                         f"You have used **{total_used_cards}/{total_allowed_cards}** cards "
                         f"and **${total_used_price:.2f}/${total_allowed_price:.2f}** this season.\n"
                         "Please enter a search term:"
                     ))
    def chk(m): return m.author == ctx.author and m.channel == ctx.channel
    term = (await bot.wait_for('message', check=chk)).content.strip()

    res = await scryfall_search(term)
    if res.get('total_cards', 0) > 25:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='Too many results; narrow search.')
    opts = res.get('data', [])
    if not opts:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No cards found.')
    if len(opts) > 1:
        menu = "\n".join(f"{i+1}. {c['name']}" for i, c in enumerate(opts))
        await clean_send(ctx.channel, title='Choose Card', description=menu)
        while True:
            resp = await bot.wait_for('message', check=chk)
            if resp.content.isdigit() and 1 <= int(resp.content) <= len(opts):
                choice = opts[int(resp.content)-1]
                break
            await clean_send(ctx.channel,
                             title='Error',
                             description='Invalid selection.')
    else:
        choice = opts[0]
    tcg = choice.get('tcgplayer_id')
    if not tcg:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No TCGplayer ID.')
    det = await scryfall_get_tcg(tcg)
    price_str = det.get('prices', {}).get('usd')
    price = float(price_str) if price_str else 0.0
    # Check cumulative allowance
    if total_used_cards + 1 > total_allowed_cards or total_used_price + price > total_allowed_price:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='Allowance exceeded for the season.')
    with open(card_csv, 'a', newline='') as f:
        csv.writer(f).writerow([wk, pid, choice['name'], tcg, f"{price:.2f}"])
    await clean_send(ctx.channel,
                     title='Card Added',
                     description=f"Added {choice['name']} — ${price:.2f}")

@bot.command(name='removecard')
async def remove_card(ctx):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    wk = str(len(data['weeks']))
    entries = [r for r in csv.DictReader(open(card_csv)) if r['week']==wk and r['user_id']==str(ctx.author.id)]
    if not entries:
        return await clean_send(ctx.channel,
                                 title='Info',
                                 description='No cards to remove.')
    menu = "\n".join(f"{i+1}. {r['card_name']} (${r['price']})" for i,r in enumerate(entries))
    await clean_send(ctx.channel,
                     title='Remove Card',
                     description=menu)
    def chk(m): return m.author==ctx.author and m.channel==ctx.channel
    while True:
        resp = await bot.wait_for('message',check=chk)
        if resp.content.isdigit() and 1<=int(resp.content)<=len(entries): idx=int(resp.content)-1; break
        await clean_send(ctx.channel,
                         title='Error',
                         description='Invalid selection.')
    to_remove = entries[idx]
    rows = [r for r in csv.DictReader(open(card_csv)) if r!=to_remove]
    with open(card_csv,'w',newline='') as f:
        w=csv.writer(f)
        w.writerow(['week','user_id','card_name','tcgplayer_id','price'])
        for r in rows:
            w.writerow([r['week'],r['user_id'],r['card_name'],r['tcgplayer_id'],r['price']])
    await clean_send(ctx.channel,
                     title='Card Removed',
                     description=f"Removed {to_remove['card_name']}")

@bot.command(name='viewcards')
async def view_cards(ctx, member: discord.Member):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    entries = {}
    for r in csv.DictReader(open(card_csv)):
        if r['user_id']==str(member.id):
            entries.setdefault(r['week'],[]).append(r)
    if not entries:
        return await clean_send(ctx.channel,
                                 title='Info',
                                 description=f"No cards for {member.display_name}.")
    caps = {'win':(1,5),'middle':(3,10),'last':(5,15)}
    lines=[]
    for wk,rs in sorted(entries.items(), key=lambda x:int(x[0])):
        wk_data = data['weeks'].get(wk, {})
        if wk_data.get('finalized'):
            info = wk_data['allowances'].get(str(member.id),{})
            c_lim, p_lim = info.get('card_limit', caps['middle'][0]), info.get('price_limit', caps['middle'][1])
            cat = info.get('category','middle')
        else:
            c_lim, p_lim = caps['middle']
            cat = 'middle'
        cnt = len(rs)
        tot = sum(float(r['price']) for r in rs)
        lines.append(f"Week {wk} ({cat.title()}): {cnt}/{c_lim} cards — ${tot:.2f}/${p_lim}")
        for r in rs:
            lines.append(f"  • {r['card_name']}: ${r['price']}")
    await clean_send(ctx.channel,
                     title=f"Cards for {member.display_name}",
                     description="\n".join(lines))

@bot.command(name='finalizeweek')
async def finalize_week_cmd(ctx):
    if data_file is None:
        return await clean_send(ctx.channel,
                                 title='Error',
                                 description='No league loaded.')
    wk = str(len(data['weeks']))
    if data['weeks'][wk].get('finalized'):
        return await clean_send(ctx.channel,
                                 title='Info',
                                 description=f"Week {wk} already finalized.")
    await finalize_week_procedures(ctx.channel, wk)

if __name__ == '__main__':
    bot.run(token)
