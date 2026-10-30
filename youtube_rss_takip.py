#!/usr/bin/env python3
"""YouTube kanallarını RSS ile takip eden, erişilebilir GTK3 uygulaması.

Kanal bağlantısı (kanal/UC..., @kullanıcı, feed bağlantısı) veya kanal kimliği
girilir; kayıtlı kanalların yeni videoları listelenir. Bir videoya Enter
tuşuyla ya da çift tıklayarak tarayıcıda açılır. Kanal sayısında sınır yoktur.

Gereksinim: python3-gi, gir1.2-gtk-3.0
"""
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id="
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "youtube-rss-takip",
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "kanallar.json")
USER_AGENT = "Mozilla/5.0 (X11; Linux) youtube-rss-takip"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_channel_id(text):
    """Kullanıcı girdisinden kanal kimliğini (UC...) bulur."""
    text = text.strip()
    if not text:
        raise ValueError("Boş giriş")
    if CHANNEL_ID_RE.match(text):
        return text
    if "://" not in text:
        if text.startswith(("youtube.com", "www.youtube.com", "m.youtube.com")):
            text = "https://" + text
        else:
            text = "https://www.youtube.com/@" + text.lstrip("@")
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    if "channel_id" in query and CHANNEL_ID_RE.match(query["channel_id"][0]):
        return query["channel_id"][0]
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", parsed.path)
    if m:
        return m.group(1)
    if "youtube.com" not in parsed.netloc:
        raise ValueError("Geçerli bir YouTube kanal bağlantısı değil")
    html = http_get(text).decode("utf-8", "replace")
    for pattern in (
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
        r'"channelId":"(UC[0-9A-Za-z_-]{22})"',
        r'"externalId":"(UC[0-9A-Za-z_-]{22})"',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise ValueError("Kanal kimliği bulunamadı")


def parse_feed(data):
    """Feed verisinden (kanal adı, [video sözlükleri]) döndürür."""
    root = ET.fromstring(data)
    title = root.findtext("a:title", default="", namespaces=NS)
    videos = []
    for entry in root.findall("a:entry", NS):
        link = entry.find("a:link", NS)
        videos.append({
            "channel": title,
            "title": entry.findtext("a:title", default="", namespaces=NS),
            "published": entry.findtext("a:published", default="", namespaces=NS),
            "url": link.get("href") if link is not None else "",
        })
    return title, videos


def fetch_channel(channel_id):
    return parse_feed(http_get(FEED_URL + channel_id))


def load_channels():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [c for c in data if isinstance(c, dict) and "id" in c]
    except (OSError, ValueError):
        return []


def save_channels(channels):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    def set_a11y(widget, name, desc=None):
        acc = widget.get_accessible()
        acc.set_name(name)
        if desc:
            acc.set_description(desc)

    class App(Gtk.Window):
        def __init__(self):
            super().__init__(title="YouTube Kanal Takip")
            self.set_default_size(900, 600)
            self.connect("destroy", Gtk.main_quit)
            self.channels = load_channels()

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_border_width(8)
            self.add(box)

            # Kanal ekleme satırı
            row = Gtk.Box(spacing=6)
            label = Gtk.Label(label="Kanal _bağlantısı:", use_underline=True)
            self.entry = Gtk.Entry(hexpand=True)
            self.entry.set_placeholder_text("https://www.youtube.com/@kanal")
            label.set_mnemonic_widget(self.entry)
            set_a11y(self.entry, "Kanal bağlantısı",
                     "YouTube kanal bağlantısı, @kullanıcı adı veya kanal kimliği")
            self.entry.connect("activate", self.on_add)
            self.add_btn = Gtk.Button.new_with_mnemonic("_Ekle")
            set_a11y(self.add_btn, "Kanal ekle")
            self.add_btn.connect("clicked", self.on_add)
            row.pack_start(label, False, False, 0)
            row.pack_start(self.entry, True, True, 0)
            row.pack_start(self.add_btn, False, False, 0)
            box.pack_start(row, False, False, 0)

            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            box.pack_start(paned, True, True, 0)

            # Kayıtlı kanallar
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            ch_label = Gtk.Label(label="_Kayıtlı kanallar", use_underline=True,
                                 xalign=0)
            self.ch_store = Gtk.ListStore(str, str)
            self.ch_view = Gtk.TreeView(model=self.ch_store)
            self.ch_view.set_headers_visible(False)
            self.ch_view.append_column(
                Gtk.TreeViewColumn("Kanal", Gtk.CellRendererText(), text=0))
            ch_label.set_mnemonic_widget(self.ch_view)
            set_a11y(self.ch_view, "Kayıtlı kanallar")
            sw = Gtk.ScrolledWindow()
            sw.add(self.ch_view)
            self.rm_btn = Gtk.Button.new_with_mnemonic("Seçili kanalı _sil")
            set_a11y(self.rm_btn, "Seçili kanalı sil")
            self.rm_btn.connect("clicked", self.on_remove)
            left.pack_start(ch_label, False, False, 0)
            left.pack_start(sw, True, True, 0)
            left.pack_start(self.rm_btn, False, False, 0)
            paned.pack1(left, False, False)

            # Videolar
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            v_label = Gtk.Label(label="_Yeni videolar", use_underline=True,
                                xalign=0)
            self.v_store = Gtk.ListStore(str, str, str, str)
            self.v_view = Gtk.TreeView(model=self.v_store)
            for i, name in enumerate(("Video", "Kanal", "Tarih")):
                col = Gtk.TreeViewColumn(name, Gtk.CellRendererText(), text=i)
                col.set_resizable(True)
                col.set_sort_column_id(i)
                self.v_view.append_column(col)
            self.v_view.set_search_column(0)
            v_label.set_mnemonic_widget(self.v_view)
            set_a11y(self.v_view, "Yeni videolar",
                     "Videoyu açmak için Enter tuşuna basın veya çift tıklayın")
            self.v_view.connect("row-activated", self.on_open)
            sw2 = Gtk.ScrolledWindow()
            sw2.add(self.v_view)
            self.refresh_btn = Gtk.Button.new_with_mnemonic("_Yenile")
            set_a11y(self.refresh_btn, "Videoları yenile")
            self.refresh_btn.connect("clicked", lambda *_: self.refresh())
            right.pack_start(v_label, False, False, 0)
            right.pack_start(sw2, True, True, 0)
            right.pack_start(self.refresh_btn, False, False, 0)
            paned.pack2(right, True, False)

            self.status = Gtk.Label(label="Hazır", xalign=0)
            set_a11y(self.status, "Durum")
            box.pack_start(self.status, False, False, 0)

            self.fill_channels()
            self.show_all()
            self.entry.grab_focus()
            if self.channels:
                self.refresh()

        def say(self, text):
            self.status.set_text(text)
            self.status.get_accessible().set_name(text)

        def fill_channels(self):
            self.ch_store.clear()
            for c in self.channels:
                self.ch_store.append([c.get("title") or c["id"], c["id"]])

        def on_add(self, *_):
            text = self.entry.get_text()
            if not text.strip():
                self.say("Lütfen bir kanal bağlantısı girin")
                return
            self.add_btn.set_sensitive(False)
            self.say("Kanal ekleniyor...")

            def work():
                try:
                    cid = resolve_channel_id(text)
                    if any(c["id"] == cid for c in self.channels):
                        GLib.idle_add(self.add_done, None, "Kanal zaten kayıtlı")
                        return
                    title, _ = fetch_channel(cid)
                    GLib.idle_add(self.add_done, {"id": cid, "title": title}, None)
                except Exception as e:
                    GLib.idle_add(self.add_done, None, "Kanal eklenemedi: %s" % e)

            threading.Thread(target=work, daemon=True).start()

        def add_done(self, channel, error):
            self.add_btn.set_sensitive(True)
            if error:
                self.say(error)
                return False
            self.channels.append(channel)
            save_channels(self.channels)
            self.fill_channels()
            self.entry.set_text("")
            self.say("%s eklendi" % (channel["title"] or channel["id"]))
            self.refresh()
            return False

        def on_remove(self, *_):
            model, it = self.ch_view.get_selection().get_selected()
            if it is None:
                self.say("Silmek için bir kanal seçin")
                return
            cid, name = model[it][1], model[it][0]
            self.channels = [c for c in self.channels if c["id"] != cid]
            save_channels(self.channels)
            self.fill_channels()
            self.say("%s silindi" % name)
            self.refresh()

        def refresh(self):
            channels = list(self.channels)
            self.say("Videolar yükleniyor...")
            self.refresh_btn.set_sensitive(False)

            def one(c):
                try:
                    return fetch_channel(c["id"]), None
                except Exception as e:
                    return None, "%s: %s" % (c.get("title") or c["id"], e)

            def work():
                videos, errors = [], []
                if channels:
                    with ThreadPoolExecutor(max_workers=8) as ex:
                        for res, err in ex.map(one, channels):
                            if err:
                                errors.append(err)
                            else:
                                videos.extend(res[1])
                videos.sort(key=lambda v: v["published"], reverse=True)
                GLib.idle_add(self.refresh_done, videos, errors)

            threading.Thread(target=work, daemon=True).start()

        def refresh_done(self, videos, errors):
            self.v_store.clear()
            for v in videos:
                self.v_store.append(
                    [v["title"], v["channel"], v["published"][:10], v["url"]])
            self.refresh_btn.set_sensitive(True)
            msg = "%d video listelendi" % len(videos)
            if errors:
                msg += "; %d kanal yüklenemedi" % len(errors)
            self.say(msg)
            if len(self.v_store):
                self.v_view.set_cursor(Gtk.TreePath.new_first())
            return False

        def on_open(self, view, path, column):
            url = view.get_model()[path][3]
            if url:
                self.say("Video açılıyor: %s" % view.get_model()[path][0])
                webbrowser.open(url)

    App()
    Gtk.main()


if __name__ == "__main__":
    main()
