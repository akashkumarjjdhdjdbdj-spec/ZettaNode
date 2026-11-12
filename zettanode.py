"""
=====================================================
  ZettaNode - ALL IN ONE LXC VPS Bot
  Made by tyrkrox_tech   |   Prefix: cb?
=====================================================

HOST SETUP (Ubuntu/Debian, root):
    snap install lxd
    lxd init --auto --storage-backend zfs     # zfs/btrfs/lvm = real disk limit
    apt install -y python3-pip lxcfs
    pip install -U discord.py

RUN:
    export DISCORD_TOKEN="bot_token"
    export SERVER_IP="your.public.ip"
    export OWNER_IDS="your_discord_id"        # comma separated
    python3 zettanode.py

DISCORD PORTAL: enable Message Content + Server Members intents.
FIREWALL: open TCP 20000-30000.

COMMANDS:
  User : cb?manage  cb?list  cb?oslist  cb?help
  Admin: cb?create @user ram cpu disk os | cb?listall | cb?delete name
         cb?suspend name | cb?unsuspend name | cb?resize name ram cpu disk
         cb?ticketpanel | cb?addadmin @u | cb?removeadmin @u
"""
import os, re, json, secrets, string, sqlite3, asyncio
import discord
from discord.ext import commands

# ================= CONFIG =================
TOKEN = os.getenv("DISCORD_TOKEN", "")
SERVER_IP = os.getenv("SERVER_IP", "YOUR.SERVER.IP")
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
PREFIX = "cb?"
BOT_NAME = "ZettaNode"
CREDIT = "Made by tyrkrox_tech"
MENU_TIMEOUT = 600          # 10 minutes, then the menu is removed
PORT_START = 20000
DB_FILE = "zettanode.db"
COLOR = 0x7C3AED

# key: (lxc image, package manager)
OS_LIST = {
    "ubuntu24":  ("images:ubuntu/24.04",  "apt"),
    "ubuntu22":  ("images:ubuntu/22.04",  "apt"),
    "ubuntu20":  ("images:ubuntu/20.04",  "apt"),
    "debian12":  ("images:debian/12",     "apt"),
    "debian11":  ("images:debian/11",     "apt"),
    "alma9":     ("images:almalinux/9",   "dnf"),
    "rocky9":    ("images:rockylinux/9",  "dnf"),
    "fedora40":  ("images:fedora/40",     "dnf"),
    "alpine":    ("images:alpine/3.20",   "apk"),
    "arch":      ("images:archlinux",     "pacman"),
}

SETUP = {
    "apt": "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y openssh-server curl sudo nano ca-certificates",
    "dnf": "dnf install -y openssh-server curl sudo nano passwd",
    "apk": "apk add --no-cache openssh curl bash sudo nano shadow && ssh-keygen -A",
    "pacman": "pacman -Sy --noconfirm openssh curl sudo nano && ssh-keygen -A",
}
SSH_CFG = (
    "mkdir -p /etc/ssh/sshd_config.d; "
    "printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-zetta.conf; "
    "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/; s/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
    "(systemctl enable --now ssh || systemctl enable --now sshd || (rc-update add sshd && rc-service sshd restart)) 2>&1"
)

# ================= DB =================
db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS vps(
    name TEXT PRIMARY KEY, owner INTEGER, os TEXT, ram INTEGER, cpu INTEGER,
    disk INTEGER, port INTEGER, suspended INTEGER DEFAULT 0)""")
db.execute("CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY)")
db.commit()


def is_admin(uid: int) -> bool:
    return uid in OWNER_IDS or db.execute("SELECT 1 FROM admins WHERE id=?", (uid,)).fetchone() is not None


def get_vps(name):
    return db.execute("SELECT * FROM vps WHERE name=?", (name,)).fetchone()


def user_vps(uid):
    return db.execute("SELECT * FROM vps WHERE owner=?", (uid,)).fetchall()


def next_port():
    r = db.execute("SELECT MAX(port) m FROM vps").fetchone()["m"]
    return (r + 1) if r else PORT_START


def gen_pw(n=16):
    a = string.ascii_letters + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


# ================= LXC helpers =================
async def sh(*args, stdin=None, timeout=600):
    p = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 1, "timeout"
    return p.returncode, out.decode(errors="ignore")


async def lxc_exec(name, *cmd, stdin=None, timeout=600):
    return await sh("lxc", "exec", name, "--", *cmd, stdin=stdin, timeout=timeout)


async def set_password(name, pw):
    return await lxc_exec(name, "chpasswd", stdin=f"root:{pw}\n", timeout=30)


async def get_state(name):
    rc, out = await sh("lxc", "query", f"/1.0/instances/{name}/state", timeout=30)
    try:
        return json.loads(out)
    except Exception:
        return None


def fmt_bytes(b):
    b = b or 0
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


async def deploy_vps(owner_id, os_key, ram, cpu, disk):
    image, pm = OS_LIST[os_key]
    name = f"zn{secrets.token_hex(3)}"
    port = next_port()
    pw = gen_pw()

    rc, out = await sh("lxc", "launch", image, name,
                       "-c", f"limits.cpu={cpu}", "-c", f"limits.memory={ram}GB",
                       "-c", "security.nesting=true",
                       "-d", f"root,size={disk}GB", timeout=900)
    if rc != 0:
        await sh("lxc", "delete", name, "--force")
        return None, f"Launch failed:\n{out[-800:]}"

    await sh("lxc", "config", "device", "add", name, "ssh", "proxy",
             f"listen=tcp:0.0.0.0:{port}", "connect=tcp:127.0.0.1:22")

    # wait for network then install ssh
    ok = False
    for _ in range(15):
        rc, _o = await lxc_exec(name, "sh", "-c", "ping -c1 -W2 1.1.1.1 >/dev/null 2>&1", timeout=20)
        if rc == 0:
            ok = True
            break
        await asyncio.sleep(3)
    if not ok:
        await sh("lxc", "delete", name, "--force")
        return None, "Container network not ready (check lxdbr0 / NAT)."

    rc, out = await lxc_exec(name, "sh", "-c", SETUP[pm] + " && " + SSH_CFG, timeout=900)
    if rc != 0:
        await sh("lxc", "delete", name, "--force")
        return None, f"Setup failed:\n{out[-800:]}"
    await set_password(name, pw)

    db.execute("INSERT INTO vps(name,owner,os,ram,cpu,disk,port) VALUES(?,?,?,?,?,?,?)",
               (name, owner_id, os_key, ram, cpu, disk, port))
    db.commit()
    return {"name": name, "port": port, "pw": pw}, None


# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def embed(title, desc=None):
    e = discord.Embed(title=title, description=desc, color=COLOR)
    e.set_footer(text=f"{BOT_NAME} • {CREDIT}")
    return e


# ---------- Manage menu ----------
class AutoView(discord.ui.View):
    """View that deletes its message after MENU_TIMEOUT seconds."""
    def __init__(self, author_id):
        super().__init__(timeout=MENU_TIMEOUT)
        self.author_id = author_id
        self.message = None

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.author_id and not is_admin(i.user.id):
            await i.response.send_message("Ye menu tumhara nahi hai.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass


class PwModal(discord.ui.Modal, title="Change VPS Password"):
    pw = discord.ui.TextInput(label="New root password (8-64, no space/colon)", min_length=8, max_length=64)

    def __init__(self, name):
        super().__init__()
        self.name = name

    async def on_submit(self, i: discord.Interaction):
        val = str(self.pw)
        if not re.fullmatch(r"[^\s:]{8,64}", val):
            return await i.response.send_message("Invalid password format.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        rc, out = await set_password(self.name, val)
        await i.followup.send("✅ Password changed." if rc == 0 else f"❌ Failed:\n{out[-500:]}", ephemeral=True)


class ManageView(AutoView):
    def __init__(self, author_id, name):
        super().__init__(author_id)
        self.name = name

    def vps(self):
        return get_vps(self.name)

    async def _guard(self, i):
        v = self.vps()
        if not v:
            await i.response.send_message("VPS not found (deleted?).", ephemeral=True)
            return None
        return v

    async def _power(self, i, action):
        v = await self._guard(i)
        if not v:
            return
        if v["suspended"] and action != "stop":
            return await i.response.send_message("⛔ VPS suspended by admin.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        args = {"start": ["start"], "stop": ["stop", "--force"], "restart": ["restart", "--force"]}[action]
        rc, out = await sh("lxc", *args, self.name, timeout=120)
        await i.followup.send(f"✅ {action} done." if rc == 0 else f"❌ {out[-500:]}", ephemeral=True)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def b_start(self, i, b): await self._power(i, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def b_stop(self, i, b): await self._power(i, "stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def b_restart(self, i, b): await self._power(i, "restart")

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary, row=1)
    async def b_stats(self, i, b):
        v = await self._guard(i)
        if not v:
            return
        await i.response.defer(ephemeral=True)
        s = await get_state(self.name)
        if not s:
            return await i.followup.send("Stats unavailable.", ephemeral=True)
        mem = s.get("memory", {}).get("usage", 0)
        disk = (s.get("disk") or {}).get("root", {}).get("usage", 0)
        cpu = s.get("cpu", {}).get("usage", 0) / 1e9
        e = embed(f"📊 {self.name}")
        e.add_field(name="Status", value=s.get("status", "?"))
        e.add_field(name="RAM", value=f"{fmt_bytes(mem)} / {v['ram']} GB")
        e.add_field(name="Disk", value=f"{fmt_bytes(disk)} / {v['disk']} GB")
        e.add_field(name="CPU", value=f"{v['cpu']} cores ({cpu:.0f}s CPU time)")
        e.add_field(name="Processes", value=str(s.get("processes", 0)))
        await i.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="SSH Info", emoji="🔑", style=discord.ButtonStyle.secondary, row=1)
    async def b_ssh(self, i, b):
        v = await self._guard(i)
        if not v:
            return
        await i.response.send_message(
            f"```ssh root@{SERVER_IP} -p {v['port']}```Password bhool gaye? **Reset Password** dabao.",
            ephemeral=True)

    @discord.ui.button(label="SSHX", emoji="🌐", style=discord.ButtonStyle.primary, row=1)
    async def b_sshx(self, i, b):
        v = await self._guard(i)
        if not v:
            return
        if v["suspended"]:
            return await i.response.send_message("⛔ VPS suspended.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await lxc_exec(self.name, "sh", "-c",
                       "pkill -f 'sshx' 2>/dev/null; rm -f /tmp/sshx.log; "
                       "setsid nohup sh -c 'curl -sSf https://sshx.io/get | sh -s run' >/tmp/sshx.log 2>&1 &",
                       timeout=30)
        link = None
        for _ in range(20):
            await asyncio.sleep(1.5)
            rc, out = await lxc_exec(self.name, "cat", "/tmp/sshx.log", timeout=15)
            m = re.search(r"https://sshx\.io/s/[^\s\x1b]+", out)
            if m:
                link = m.group(0)
                break
        await i.followup.send(f"🌐 SSHX: {link}" if link else "❌ SSHX link nahi mila. VPS running hai? Internet check karo.",
                              ephemeral=True)

    @discord.ui.button(label="Reset Password", emoji="♻️", style=discord.ButtonStyle.secondary, row=2)
    async def b_reset(self, i, b):
        v = await self._guard(i)
        if not v:
            return
        await i.response.defer(ephemeral=True)
        pw = gen_pw()
        rc, out = await set_password(self.name, pw)
        await i.followup.send(f"✅ New password: ||{pw}||" if rc == 0 else f"❌ {out[-500:]}", ephemeral=True)

    @discord.ui.button(label="Change Password", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def b_change(self, i, b):
        if not await self._guard(i):
            return
        await i.response.send_modal(PwModal(self.name))

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def b_close(self, i, b):
        await i.response.defer()
        try:
            await i.message.delete()
        except Exception:
            pass
        self.stop()


def vps_embed(v):
    e = embed(f"🖥️ {v['name']}", f"OS: `{v['os']}`")
    e.add_field(name="RAM", value=f"{v['ram']} GB")
    e.add_field(name="CPU", value=f"{v['cpu']} cores")
    e.add_field(name="Disk", value=f"{v['disk']} GB")
    e.add_field(name="SSH", value=f"`ssh root@{SERVER_IP} -p {v['port']}`", inline=False)
    if v["suspended"]:
        e.add_field(name="Status", value="⛔ Suspended", inline=False)
    e.add_field(name="Auto close", value="Ye menu 10 min me hat jayega.", inline=False)
    return e


class PickSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Select VPS", options=[
            discord.SelectOption(label=r["name"], description=f"{r['os']} • {r['ram']}GB/{r['cpu']}CPU/{r['disk']}GB")
            for r in rows[:25]])

    async def callback(self, i: discord.Interaction):
        v = get_vps(self.values[0])
        view = ManageView(self.view.author_id, v["name"])
        view.message = self.view.message
        self.view.stop()
        await i.response.edit_message(embed=vps_embed(v), view=view)


class PickView(AutoView):
    def __init__(self, author_id, rows):
        super().__init__(author_id)
        self.add_item(PickSelect(rows))


@bot.command(name="manage")
async def manage(ctx, name: str = None):
    if name:
        v = get_vps(name)
        if not v or (v["owner"] != ctx.author.id and not is_admin(ctx.author.id)):
            return await ctx.send("VPS nahi mila ya access nahi hai.")
        view = ManageView(ctx.author.id, v["name"])
        view.message = await ctx.send(embed=vps_embed(v), view=view)
        return
    rows = user_vps(ctx.author.id)
    if not rows:
        return await ctx.send("Tumhare paas koi VPS nahi hai.")
    if len(rows) == 1:
        view = ManageView(ctx.author.id, rows[0]["name"])
        view.message = await ctx.send(embed=vps_embed(rows[0]), view=view)
    else:
        view = PickView(ctx.author.id, rows)
        view.message = await ctx.send(embed=embed("Select your VPS"), view=view)


# ---------- Create / admin ----------
@bot.command(name="create")
async def create(ctx, user: discord.Member = None, ram: int = None, cpu: int = None, disk: int = None, os_key: str = None):
    if not is_admin(ctx.author.id):
        return await ctx.send("❌ Sirf admin VPS create kar sakta hai.")
    if None in (user, ram, cpu, disk, os_key):
        return await ctx.send(f"Usage: `{PREFIX}create @user <ram_gb> <cpu> <disk_gb> <os>`\nOS: `{', '.join(OS_LIST)}`")
    os_key = os_key.lower()
    if os_key not in OS_LIST:
        return await ctx.send(f"OS list: `{', '.join(OS_LIST)}`")
    if not (1 <= ram <= 128 and 1 <= cpu <= 64 and 1 <= disk <= 2000):
        return await ctx.send("Invalid resources.")
    msg = await ctx.send(embed=embed("⏳ Creating VPS...", "1-3 minute lag sakte hain."))
    res, err = await deploy_vps(user.id, os_key, ram, cpu, disk)
    if err:
        return await msg.edit(embed=embed("❌ Failed", f"```{err[-900:]}```"))
    e = embed("✅ VPS Ready", f"Owner: {user.mention}")
    e.add_field(name="Name", value=res["name"])
    e.add_field(name="Specs", value=f"{ram}GB RAM • {cpu} CPU • {disk}GB Disk")
    await msg.edit(embed=e)
    creds = embed("🖥️ Your ZettaNode VPS")
    creds.add_field(name="SSH", value=f"```ssh root@{SERVER_IP} -p {res['port']}```", inline=False)
    creds.add_field(name="Password", value=f"||{res['pw']}||")
    creds.add_field(name="Manage", value=f"`{PREFIX}manage`")
    try:
        await user.send(embed=creds)
    except discord.Forbidden:
        await ctx.author.send(content=f"{user} ke DM band hain, creds:", embed=creds)


@bot.command(name="oslist")
async def oslist(ctx):
    await ctx.send(embed=embed("Available OS", "\n".join(f"`{k}` → {v[0]}" for k, v in OS_LIST.items())))


@bot.command(name="list")
async def list_own(ctx):
    rows = user_vps(ctx.author.id)
    await ctx.send(embed=embed("Your VPS", "\n".join(f"`{r['name']}` {r['os']} {r['ram']}GB/{r['cpu']}CPU/{r['disk']}GB" for r in rows) or "None"))


@bot.command(name="listall")
async def listall(ctx):
    if not is_admin(ctx.author.id):
        return
    rows = db.execute("SELECT * FROM vps").fetchall()
    txt = "\n".join(f"`{r['name']}` <@{r['owner']}> {r['os']} {r['ram']}GB/{r['cpu']}CPU/{r['disk']}GB :{r['port']}"
                    + (" ⛔" if r["suspended"] else "") for r in rows) or "None"
    await ctx.send(embed=embed("All VPS", txt[:4000]))


@bot.command(name="delete")
async def delete(ctx, name: str = None):
    if not is_admin(ctx.author.id) or not name or not get_vps(name):
        return await ctx.send("Admin only / VPS not found.")
    await sh("lxc", "delete", name, "--force")
    db.execute("DELETE FROM vps WHERE name=?", (name,))
    db.commit()
    await ctx.send(f"🗑️ `{name}` deleted.")


@bot.command(name="suspend")
async def suspend(ctx, name: str = None):
    if not is_admin(ctx.author.id) or not name or not get_vps(name):
        return await ctx.send("Admin only / VPS not found.")
    await sh("lxc", "stop", name, "--force")
    db.execute("UPDATE vps SET suspended=1 WHERE name=?", (name,))
    db.commit()
    await ctx.send(f"⛔ `{name}` suspended.")


@bot.command(name="unsuspend")
async def unsuspend(ctx, name: str = None):
    if not is_admin(ctx.author.id) or not name or not get_vps(name):
        return await ctx.send("Admin only / VPS not found.")
    db.execute("UPDATE vps SET suspended=0 WHERE name=?", (name,))
    db.commit()
    await sh("lxc", "start", name)
    await ctx.send(f"✅ `{name}` unsuspended.")


@bot.command(name="addadmin")
async def addadmin(ctx, user: discord.Member = None):
    if ctx.author.id not in OWNER_IDS or not user:
        return await ctx.send("Owner only.")
    db.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (user.id,))
    db.commit()
    await ctx.send(f"✅ {user.mention} is now admin.")


@bot.command(name="removeadmin")
async def removeadmin(ctx, user: discord.Member = None):
    if ctx.author.id not in OWNER_IDS or not user:
        return await ctx.send("Owner only.")
    db.execute("DELETE FROM admins WHERE id=?", (user.id,))
    db.commit()
    await ctx.send(f"✅ {user.mention} removed from admin.")


@bot.command(name="resize")
async def resize(ctx, name: str = None, ram: int = None, cpu: int = None, disk: int = None):
    if not is_admin(ctx.author.id) or None in (name, ram, cpu, disk) or not get_vps(name):
        return await ctx.send(f"Admin only. Usage: `{PREFIX}resize <name> <ram> <cpu> <disk>`")
    await sh("lxc", "config", "set", name, f"limits.memory={ram}GB", f"limits.cpu={cpu}")
    await sh("lxc", "config", "device", "set", name, "root", f"size={disk}GB")
    db.execute("UPDATE vps SET ram=?,cpu=?,disk=? WHERE name=?", (ram, cpu, disk, name))
    db.commit()
    await ctx.send(f"✅ `{name}` resized.")


# ---------- Tickets ----------
class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="zn_ticket_open")
    async def open_ticket(self, i: discord.Interaction, b):
        g = i.guild
        cname = f"ticket-{i.user.id}"
        if discord.utils.get(g.text_channels, name=cname):
            return await i.response.send_message("Tumhara ticket pehle se open hai.", ephemeral=True)
        cat = discord.utils.get(g.categories, name="Tickets") or await g.create_category("Tickets")
        ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
              i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
              g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
        for aid in OWNER_IDS | {r["id"] for r in db.execute("SELECT id FROM admins")}:
            m = g.get_member(aid)
            if m:
                ow[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        ch = await g.create_text_channel(cname, category=cat, overwrites=ow)
        await ch.send(i.user.mention, embed=embed("🎫 Support Ticket", "Apni problem likho, admin jaldi reply karega."),
                      view=TicketClose())
        await i.response.send_message(f"✅ Ticket: {ch.mention}", ephemeral=True)


class TicketClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="zn_ticket_close")
    async def close(self, i: discord.Interaction, b):
        is_owner = i.channel.name == f"ticket-{i.user.id}"
        if not is_owner and not is_admin(i.user.id):
            return await i.response.send_message("Not allowed.", ephemeral=True)
        await i.response.send_message("Closing in 5 seconds...")
        await asyncio.sleep(5)
        await i.channel.delete()


@bot.command(name="ticketpanel")
async def ticketpanel(ctx):
    if not is_admin(ctx.author.id):
        return
    await ctx.send(embed=embed("🎫 ZettaNode Support", "Help chahiye? Niche button dabao."), view=TicketPanel())


# ---------- Help ----------
@bot.command(name="help")
async def help_cmd(ctx):
    e = embed(f"{BOT_NAME} Commands")
    e.add_field(name="User", value=f"`{PREFIX}manage` • `{PREFIX}list` • `{PREFIX}oslist`", inline=False)
    e.add_field(name="Admin", value=(f"`{PREFIX}create @user ram cpu disk os`\n`{PREFIX}listall` • `{PREFIX}delete name`\n"
                                    f"`{PREFIX}suspend name` • `{PREFIX}unsuspend name`\n`{PREFIX}resize name ram cpu disk`\n"
                                    f"`{PREFIX}ticketpanel` • `{PREFIX}addadmin @u` • `{PREFIX}removeadmin @u`"), inline=False)
    await ctx.send(embed=e)


@bot.event
async def setup_hook():
    bot.add_view(TicketPanel())
    bot.add_view(TicketClose())


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(f"{PREFIX}help | {BOT_NAME}"))
    print(f"{BOT_NAME} online as {bot.user} | {CREDIT}")


if __name__ == "__main__":
    bot.run(TOKEN)
