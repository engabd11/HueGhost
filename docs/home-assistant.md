# Home Assistant

Expose the control API to the LAN first (`hue-ghost setup` asks, or set
`control.bind` to `0.0.0.0` and `control.token` to a random string and
restart). Note the PC's IP; the port is `control.port` (8787).

## Option A - Hue Synco "Movie mode" (recommended)

[Hue Synco](https://github.com/engabd11/syncoV2) (HACS) already streams music
sync to Hue entertainment areas from Home Assistant. From v1.57 it can also
drive hue-ghost:

Settings > Devices & services > Hue Synco > **Configure** > fill in
*Hue Ghost host / port / token*. A **Hue Ghost** device appears with:

| entity | |
|---|---|
| `light.hue_ghost_global_sync` | the master control: on/off is movie mode, brightness is the level Hue Sync runs the area at |
| `sensor.hue_ghost_sync_status` | `offline` / `idle` / `ghosting` / `syncing`, attributes: now playing, position, drift, engine state |
| `sensor.hue_ghost_sync_area` | the entertainment area the sync plays in |
| `sensor.hue_ghost_active_source` | which followed source is driving the lights now |
| `sensor.hue_ghost_now_playing` | the Jellyfin item, or what the PC source reported |
| `select.hue_ghost_mode` | video / music / games - what Hue Sync reacts to |
| `select.hue_ghost_intensity` | subtle / moderate / high / extreme |
| `switch.hue_ghost_<source>` | one per binding ("Apple TV", "PC"): follow it, or ignore it |
| `switch.hue_ghost_audio_effects` | "use audio for light effects" in video/games mode |
| `number.hue_ghost_sync_offset` | the lead in seconds, live (tune from the couch) |
| `sensor.hue_ghost_sync_drift` | diagnostic: the measured ghost-vs-TV gap the offset cancels |

Hue Synco ships a **Hue Ghost Card (Movie mode)** that puts the lot on one
tile - the light, the intensity and mode pickers, and a tile per source - in
this app's own gold-on-charcoal. Add `type: custom:hue-ghost-card` to a
dashboard; it finds the device itself.

Mode and intensity apply live, mid-movie - switching Video to Music or Games
never restarts the Hue Sync app or drops the sync. They also **mirror** it:
change the mode or the intensity in Hue Sync itself and the selects follow,
because hue-ghost adopts your choice instead of asserting its own. (Hue Sync
keeps an intensity per mode, so the one hue-ghost is set to is re-applied when
the mode changes; only an intensity you pick yourself becomes the new setting.)

The audio switch is the one setting the Hue Sync app only reads at start-up, so
it restarts the app (~3 s); while movie mode is off it just remembers your
choice. Its state shows the app's own setting until you pick one.

Turning movie mode **on** first stops any active music-sync area (the bridge
allows one streamer per entertainment area), then enables hue-ghost.
Starting a music-sync area while movie mode is on turns movie mode off.

Automation ideas: movie mode on at sunset and off at bedtime; a dashboard
button next to the music-sync card; `sensor.hue_ghost_sync_status == syncing`
to dim other lights; `sensor.hue_ghost_sync_area` to key an automation to the
room the sync actually landed in.

## Option B - plain REST (no integration)

```yaml
# configuration.yaml
switch:
  - platform: rest
    name: Hue Ghost movie mode
    # the switch posts to `resource` (/set takes {"enabled": bool}) and reads
    # its state from `state_resource` (/status ignores a body)
    resource: http://192.168.0.50:8787/set
    state_resource: http://192.168.0.50:8787/status
    body_on: '{"enabled": true}'
    body_off: '{"enabled": false}'
    is_on_template: "{{ value_json.enabled }}"
    headers:
      Authorization: "Bearer YOUR_TOKEN"
      Content-Type: application/json
    method: post

rest_command:
  hue_ghost_offset_plus:
    url: http://192.168.0.50:8787/set
    method: post
    headers: { Authorization: "Bearer YOUR_TOKEN" }
    content_type: application/json
    payload: '{"offset_delta": 0.25}'
  # /set also takes {"mode": "video"|"music"|"games"},
  # {"intensity": "subtle".."extreme"},
  # {"use_audio": true|false|null}  (null = leave the Hue Sync app's own setting),
  # {"enabled": bool}, {"offset_s": s}, {"offset_delta": s},
  # {"brightness": 0-100}, {"brightness_step": n},
  # {"binding": {"key": "<id>", "enabled": bool}}, {"manage_area": bool},
  # {"keep_awake": "off"|"playing"|"always"}, {"pause_stop_min": min},
  # {"music_pause_stop_s": s}, {"lights_off_delay_s": s},
  # {"pixel_shift_min": min}, {"blackout_idle_min": min} and
  # {"time_lock": bool}  (experimental)
  hue_ghost_music_mode:
    url: http://192.168.0.50:8787/set
    method: post
    headers: { Authorization: "Bearer YOUR_TOKEN" }
    content_type: application/json
    payload: '{"mode": "music", "use_audio": true}'

sensor:
  - platform: rest
    name: Hue Ghost state
    resource: http://192.168.0.50:8787/status
    headers: { Authorization: "Bearer YOUR_TOKEN" }
    value_template: "{{ value_json.state }}"
    json_attributes_path: "$.follow"
    json_attributes: [item, position_s, paused]
    scan_interval: 5
```

Replace `192.168.0.50` with the PC's IP and `YOUR_TOKEN` with `control.token`.
