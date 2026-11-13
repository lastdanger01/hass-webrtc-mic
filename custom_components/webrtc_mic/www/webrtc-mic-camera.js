/** Chrome 63+, Safari 11.1+ */
import {VideoRTC} from './video-rtc.js?v=1.9.19-mic6';
import {DigitalPTZ} from './digital-ptz.js?v=3.3.0';

class WebRTCCamera extends VideoRTC {
    /**
     * Step 1. Called by the Hass, when config changed.
     * @param {Object} config
     */
    setConfig(config) {
        if (!config.url && !config.entity && !config.streams) throw new Error('Missing `url` or `entity` or `streams`');

        if (config.background) this.background = config.background;

        if (config.intersection === 0) this.visibilityThreshold = 0;
        else this.visibilityThreshold = config.intersection || 0.75;

        /**
         * @type {{
         *     url: string,
         *     entity: string,
         *     mode: string,
         *     media: string,
         *
         *     streams: Array<{
         *         name: string,
         *         url: string,
         *         entity: string,
         *         mode: string,
         *         media: string,
         *     }>,
         *
         *     title: string,
         *     poster: string,
         *     poster_remote: boolean,
         *     muted: boolean,
         *     intersection: number,
         *     ui: boolean,
         *     style: string,
         *     background: boolean,
         *
         *     server: string,
         *
         *     mse: boolean,
         *     webrtc: boolean,
         *
         *     digital_ptz:{
         *         mouse_drag_pan: boolean,
         *         mouse_wheel_zoom: boolean,
         *         mouse_double_click_zoom: boolean,
         *         touch_pinch_zoom: boolean,
         *         touch_drag_pan: boolean,
         *         touch_tap_drag_zoom: boolean,
         *         persist: boolean|string,
         *     },
         *     ptz:{
         *         opacity: number|string,
         *         service: string,
         *         data_left, data_up, data_right, data_down, data_zoom_in, data_zoom_out, data_home
         *     },
         *     shortcuts:Array<{ name:string, icon:string }>,
         * }} config
         */
        this.config = Object.assign({
            mode: config.mse === false ? 'webrtc' : config.webrtc === false ? 'mse' : this.mode,
            media: this.media,
            // The control bar holds the microphone button, and renderCustomUI()
            // starts with `if (!this.config.ui) return;` - without `ui: true`
            // the whole bar (and with it the microphone) is never created.
            // Default it on so the microphone exists without any card
            // configuration. An explicit `ui:` in the card still wins,
            // because Object.assign copies the card config over these
            // defaults.
            ui: true,
            streams: [{url: config.url, entity: config.entity}],
            poster_remote: config.poster && (config.poster.indexOf('://') > 0 || config.poster.charAt(0) === '/'),
        }, config);

        this.streamID = -1;
        this.nextStream(false);

        this.onhass = [];
    }

    set hass(hass) {
        this._hass = hass;
        this.onhass.forEach(fn => fn());
        // if card in vertical stack - `hass` property assign after `onconnect`
        // this.onconnect();
    }

    get hass() {
        return this._hass;
    }

    /**
     * Called by the Hass to calculate default card height.
     */
    getCardSize() {
        return 5; // x 50px
    }

    /**
     * Called by the Hass to get defaul card config
     * @return {{url: string}}
     */
    static getStubConfig() {
        return {'url': ''};
    }

    setStatus(mode, status) {
        const divMode = this.querySelector('.mode').innerText;
        if (mode === 'error' && divMode !== 'Loading..' && divMode !== 'Loading...') return;

        this.querySelector('.mode').innerText = mode;
        this.querySelector('.status').innerText = status || '';
    }

    /** @param reload {boolean} */
    nextStream(reload) {
        this.streamID = (this.streamID + 1) % this.config.streams.length;

        const stream = this.config.streams[this.streamID];
        this.config.url = stream.url;
        this.config.entity = stream.entity;
        this.mode = stream.mode || this.config.mode;
        this.media = stream.media || this.config.media;

        if (reload) {
            this.ondisconnect();
            setTimeout(() => this.onconnect(), 100); // wait ws.close event
        }
    }

    /** @return {string} */
    get streamName() {
        return this.config.streams[this.streamID].name || `S${this.streamID}`;
    }

    oninit() {
        super.oninit();
        this.renderMain();
        this.renderDigitalPTZ();
        this.renderPTZ();
        this.renderCustomUI();
        this.renderMusic();
        this.renderShortcuts();
        this.renderStyle();
    }

    onconnect() {
        if (!this.config || !this.hass) return false;
        if (!this.isConnected || this.ws || this.pc) return false;

        const divMode = this.querySelector('.mode').innerText;
        if (divMode === 'Loading..') return;

        this.setStatus('Loading..');

        this.hass.callWS({
            type: 'auth/sign_path', path: '/api/webrtc_mic/ws'
        }).then(data => {
            if (this.config.poster && !this.config.poster_remote) {
                this.video.poster = this.hass.hassUrl(data.path) + '&poster=' + encodeURIComponent(this.config.poster);
            }

            this.wsURL = 'ws' + this.hass.hassUrl(data.path).substring(4);

            if (this.config.entity) {
                this.wsURL += '&entity=' + this.config.entity;
            } else if (this.config.url) {
                this.wsURL += '&url=' + encodeURIComponent(this.config.url);
            } else {
                this.setStatus('IMG');
                return;
            }

            if (this.config.server) {
                this.wsURL += '&server=' + encodeURIComponent(this.config.server);
            }

            if (super.onconnect()) {
                this.setStatus('Loading...');
            } else {
                this.setStatus('error', 'unable to connect');
            }
        }).catch(er => {
            this.setStatus('error', er);
        });
    }

    onopen() {
        const result = super.onopen();

        this.onmessage['stream'] = msg => {
            switch (msg.type) {
                case 'error':
                    this.setStatus('error', msg.value);
                    break;
                case 'mse':
                case 'hls':
                case 'mp4':
                case 'mjpeg':
                    this.setStatus(msg.type.toUpperCase(), this.config.title || '');
                    break;
            }
        };

        return result;
    }

    onpcvideo(ev) {
        super.onpcvideo(ev);

        if (this.pcState !== WebSocket.CLOSED) {
            this.setStatus('RTC', this.config.title || '');
        }
    }

    renderMain() {
        const shadow = this.attachShadow({mode: 'open'});
        shadow.innerHTML = `
        <style>
            ha-card {
                width: 100%;
                height: 100%;
                margin: auto;
                overflow: hidden;
                position: relative;
            }
            ha-icon {
                color: white;
                cursor: pointer;
            }
            .player {
                background-color: black;
                height: 100%;
                position: relative; /* important for Safari */
            }
            .player:active {
                cursor: move; /* important for zoom-controller */
            }
            .player .ptz-transform {
                height: 100%;
            }
            .header {
                position: absolute;
                top: 6px;
                left: 10px;
                right: 10px;
                color: white;
                display: flex;
                justify-content: space-between;
                pointer-events: none;
            }
            .mode {
                cursor: pointer;
                opacity: 0.6;
                pointer-events: auto;
            }
        </style>
        <ha-card class="card">
            <div class="player">
                <div class="ptz-transform"></div>
            </div>
            <div class="header">
                <div class="status"></div>
                <div class="mode"></div>
            </div>
        </ha-card>
        `;

        this.querySelector = selectors => this.shadowRoot.querySelector(selectors);
        this.querySelector('.ptz-transform').appendChild(this.video);

        const mode = this.querySelector('.mode');
        mode.addEventListener('click', () => this.nextStream(true));

        if (this.config.muted) this.video.muted = true;
        if (this.config.poster_remote) this.video.poster = this.config.poster;
        // without a poster, Android WebView draws its grey play-button placeholder whenever the video has
        // no frame (before the first one and while switching MSE -> WebRTC). A transparent image hides it;
        // 300x150 is the default size of an empty video, so the layout while loading stays the same
        if (!this.config.poster) this.video.poster = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='150'/%3E";
    }

    renderDigitalPTZ() {
        if (this.config.digital_ptz === false) return;
        new DigitalPTZ(
            this.querySelector('.player'),
            this.querySelector('.player .ptz-transform'),
            this.video,
            Object.assign({}, this.config.digital_ptz, {persist_key: this.config.url})
        );
    }

    renderPTZ() {
        if (!this.config.ptz || !this.config.ptz.service) return;

        let hasMove = false;
        let hasZoom = false;
        let hasHome = false;
        for (const prefix of ['', '_start', '_end', '_long']) {
            hasMove = hasMove || this.config.ptz['data' + prefix + '_right'];
            hasMove = hasMove || this.config.ptz['data' + prefix + '_left'];
            hasMove = hasMove || this.config.ptz['data' + prefix + '_up'];
            hasMove = hasMove || this.config.ptz['data' + prefix + '_down'];

            hasZoom = hasZoom || this.config.ptz['data' + prefix + '_zoom_in'];
            hasZoom = hasZoom || this.config.ptz['data' + prefix + '_zoom_out'];

            hasHome = hasHome || this.config.ptz['data' + prefix + '_home'];
        }

        const card = this.querySelector('.card');
        card.insertAdjacentHTML('beforebegin', `
            <style>
                .ptz {
                    position: absolute;
                    top: 50%;
                    right: 10px;
                    transform: translateY(-50%);
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    transition: opacity .3s ease-in-out;
                    opacity: ${parseFloat(this.config.ptz.opacity) || 0.4};
                }
                .ptz:hover {
                    opacity: 1 !important;
                }
                .ptz-move {
                    position: relative;
                    background-color: rgba(0, 0, 0, 0.3);
                    border-radius: 50%;
                    width: 80px;
                    height: 80px;
                    display: ${hasMove ? 'block' : 'none'};
                }
                .ptz-zoom {
                    position: relative;
                    width: 80px;
                    height: 40px;
                    background-color: rgba(0, 0, 0, 0.3);
                    border-radius: 4px;
                    display: ${hasZoom ? 'block' : 'none'};
                }
                .ptz-home {
                    position: relative;
                    width: 40px;
                    height: 40px;
                    background-color: rgba(0, 0, 0, 0.3);
                    border-radius: 4px;
                    align-self: center;
                    display: ${hasHome ? 'block' : 'none'};
                }
                .up {
                    position: absolute;
                    top: 5px;
                    left: 50%;
                    transform: translateX(-50%);
                }
                .down {
                    position: absolute;
                    bottom: 5px;
                    left: 50%;
                    transform: translateX(-50%);
                }
                .left {
                    position: absolute;
                    left: 5px;
                    top: 50%;
                    transform: translateY(-50%);
                }
                .right {
                    position: absolute;
                    right: 5px;
                    top: 50%;
                    transform: translateY(-50%);
                }
                .zoom_out {
                    position: absolute;
                    left: 5px;
                    top: 50%;
                    transform: translateY(-50%);
                }
                .zoom_in {
                    position: absolute;
                    right: 5px;
                    top: 50%;
                    transform: translateY(-50%);
                }
                .home {
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                }
            </style>
        `);
        card.insertAdjacentHTML('beforeend', `
            <div class="ptz">
                <div class="ptz-move">
                    <ha-icon class="right" icon="mdi:arrow-right"></ha-icon>
                    <ha-icon class="left" icon="mdi:arrow-left"></ha-icon>
                    <ha-icon class="up" icon="mdi:arrow-up"></ha-icon>
                    <ha-icon class="down" icon="mdi:arrow-down"></ha-icon>
                </div>
                <div class="ptz-zoom">
                    <ha-icon class="zoom_in" icon="mdi:plus"></ha-icon>
                    <ha-icon class="zoom_out" icon="mdi:minus"></ha-icon>
                </div>
                <div class="ptz-home">
                    <ha-icon class="home" icon="mdi:home"></ha-icon>
                </div>
            </div>
        `);

        const template = JSON.stringify(this.config.ptz);
        const handle = path => {
            if (!this.config.ptz['data_' + path]) return;
            const config = template.indexOf('${') < 0 ? this.config.ptz : JSON.parse(eval('`' + template + '`'));
            const [domain, service] = config.service.split('.', 2);
            const data = config['data_' + path];
            this.hass.callService(domain, service, data);
        };
        const ptz = this.querySelector('.ptz');
        for (const [start, end] of [['touchstart', 'touchend'], ['mousedown', 'mouseup']]) {
            ptz.addEventListener(start, startEvt => {
                const {className} = startEvt.target;
                startEvt.preventDefault();
                handle('start_' + className);
                window.addEventListener(end, endEvt => {
                    endEvt.preventDefault();
                    handle('end_' + className);
                    if (endEvt.timeStamp - startEvt.timeStamp > 400) {
                        handle('long_' + className);
                    } else {
                        handle(className);
                    }
                }, {once: true});
            });
        }
    }

    saveScreenshot() {
        const a = document.createElement('a');

        if (this.video.videoWidth && this.video.videoHeight) {
            const canvas = document.createElement('canvas');
            canvas.width = this.video.videoWidth;
            canvas.height = this.video.videoHeight;
            canvas.getContext('2d').drawImage(this.video, 0, 0, canvas.width, canvas.height);
            a.href = canvas.toDataURL('image/jpeg');
        } else if (this.video.poster && this.video.poster.startsWith('data:image/jpeg')) {
            a.href = this.video.poster;
        } else {
            return;
        }

        const ts = new Date().toISOString().substring(0, 19).replaceAll('-', '').replaceAll(':', '');
        a.download = `snapshot_${ts}.jpeg`;
        a.click();
    }

    renderCustomUI() {
        if (!this.config.ui) return;

        this.video.controls = false;
        this.video.style.pointerEvents = 'none';

        const card = this.querySelector('.card');
        card.insertAdjacentHTML('beforebegin', `
            <style>
                .spinner {
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                }
                .controls {
                    position: absolute;
                    left: 5px;
                    right: 5px;
                    bottom: 5px;
                    display: flex;
                    align-items: center;
                }
                .space {
                    width: 100%;
                }
                .volume {
                    display: none;
                }
                .microphone {
                    display: none;
                    color: #ff5252;
                }
                .microphone.active {
                    color: #00c853;
                }
                .stream {
                    padding-top: 2px;
                    margin-left: 2px;
                    font-weight: 400;
                    font-size: 20px;
                    color: white;
                    display: none;
                    cursor: pointer;
                }
                .viewers {
                    display: none;
                    color: white;
                    align-items: center;
                    gap: 2px;
                    padding-right: 6px;
                    font-size: 15px;
                }
                .viewers .count {
                    line-height: 1;
                }
                .viewers.busy {
                    opacity: 0.5;
                }
                .music {
                    display: none;
                    color: white;
                }
                .music.playing {
                    color: #00c853;
                }
                .music-picker {
                    position: absolute;
                    left: 5px;
                    right: 5px;
                    bottom: 40px;
                    max-height: 60%;
                    overflow-y: auto;
                    background: rgba(0, 0, 0, 0.85);
                    border-radius: 8px;
                    padding: 6px 0;
                    display: none;
                    z-index: 5;
                }
                .music-picker.open {
                    display: block;
                }
                .music-picker .item {
                    display: block;
                    width: 100%;
                    text-align: left;
                    padding: 8px 12px;
                    color: white;
                    font-size: 15px;
                    background: none;
                    border: none;
                    cursor: pointer;
                    font-family: inherit;
                }
                .music-picker .item:hover {
                    background: rgba(255, 255, 255, 0.15);
                }
                .music-picker .head {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 4px 12px 8px 12px;
                    color: #b0b0b0;
                    font-size: 13px;
                }
                .music-picker .head ha-icon {
                    color: #b0b0b0;
                }
                .music-picker .empty {
                    padding: 8px 12px;
                    color: #b0b0b0;
                    font-size: 14px;
                }
            </style>
        `);
        card.insertAdjacentHTML('beforeend', `
            <div class="ui">
                <ha-circular-progress class="spinner"></ha-circular-progress>
                <div class="controls">
                    <ha-icon class="fullscreen" icon="mdi:fullscreen"></ha-icon>
                    <ha-icon class="screenshot" icon="mdi:floppy"></ha-icon>
                    <ha-icon class="pictureinpicture" icon="mdi:picture-in-picture-bottom-right"></ha-icon>
                    <span class="stream">${this.streamName}</span>
                    <span class="space"></span>
                    <ha-icon class="microphone" icon="mdi:microphone-off"></ha-icon>
                    <span class="viewers" title="目前有幾個人在看攝影機">
                        <ha-icon icon="mdi:account-multiple"></ha-icon>
                        <span class="count"></span>
                    </span>
                    <ha-icon class="music" icon="mdi:music-note"></ha-icon>
                <ha-icon class="play" icon="mdi:play"></ha-icon>
                    <ha-icon class="volume" icon="mdi:volume-high"></ha-icon>
                </div>
            </div>
        `);

        const video = this.video;

        const fullscreen = this.querySelector('.fullscreen');
        if (this.requestFullscreen) {
            this.addEventListener('fullscreenchange', () => {
                fullscreen.icon = document.fullscreenElement ? 'mdi:fullscreen-exit' : 'mdi:fullscreen';
            });
        } else if (video.webkitEnterFullscreen) {
            this.requestFullscreen = () => new Promise((resolve, reject) => {
                try {
                    video.webkitEnterFullscreen();
                } catch (e) {
                    reject(e);
                }
            });
            video.addEventListener('webkitendfullscreen', () => {
                setTimeout(() => this.play(), 1000); // fix bug in iOS
            });
        } else {
            fullscreen.style.display = 'none';
        }

        const pip = this.querySelector('.pictureinpicture');
        if (video.requestPictureInPicture) {
            video.addEventListener('enterpictureinpicture', () => {
                pip.icon = 'mdi:rectangle';
                this.background = true;
            });
            video.addEventListener('leavepictureinpicture', () => {
                pip.icon = 'mdi:picture-in-picture-bottom-right';
                this.background = this.config.background;
                this.play();
            });
        } else {
            pip.style.display = 'none';
        }

        const ui = this.querySelector('.ui');
        const mic = this.querySelector('.microphone');
        if (this.config.microphone !== false) {
            mic.style.display = 'block';
        }

        // The control bar is rebuilt whenever the card is rendered again.
        // The microphone button always starts in the "off" icon, so the
        // icon has to follow the real state - otherwise a card that is
        // re-rendered while the microphone is on looks like it can never
        // be turned off.
        if (this.micEnabled) {
            mic.icon = 'mdi:microphone';
            mic.classList.add('active');
        } else {
            mic.icon = 'mdi:microphone-off';
            mic.classList.remove('active');
        }

        // Show how many people are watching the cameras. The count lives in
        // the control bar next to the microphone, so it has to be fetched
        // from the same origin as the dashboard - the browser cannot reach
        // go2rtc directly (plain HTTP under an HTTPS page = mixed content).
        this.renderViewers();

        // The browser blocks autoplay with sound and silently mutes the video
        // element. Any user gesture on the card is a chance to restore sound.
        this.addEventListener('click', () => {
            if (this.unmuteAutoplay()) {
                this.querySelector('.volume').icon = this.video.muted ? 'mdi:volume-mute' : 'mdi:volume-high';
            }
        });
        ui.addEventListener('click', ev => {
            const icon = ev.target.icon;
            if (icon === 'mdi:play') {
                this.play();
            } else if (icon === 'mdi:volume-mute') {
                video.muted = false;
            } else if (icon === 'mdi:volume-high') {
                video.muted = true;
            } else if (icon === 'mdi:fullscreen') {
                this.requestFullscreen().catch(console.warn);
            } else if (icon === 'mdi:fullscreen-exit') {
                document.exitFullscreen().catch(console.warn);
            } else if (icon === 'mdi:floppy') {
                this.saveScreenshot();
            } else if (icon === 'mdi:picture-in-picture-bottom-right') {
                video.requestPictureInPicture().catch(console.warn);
            } else if (icon === 'mdi:rectangle') {
                document.exitPictureInPicture().catch(console.warn);
            } else if (icon === 'mdi:microphone-off' || icon === 'mdi:microphone') {
                // Toggle the microphone.
                //
                // The branch used to be keyed on the icon string only, and it
                // only handled the "off" icon. Clicking the button while the
                // microphone was active then did nothing at all, so it
                // looked stuck.
                //
                // The real state is used now, not the icon: the icon is
                // derived from the state, so a stale icon can never leave
                // the microphone stuck on.
                //
                // The track is stopped, not just muted, so the browser stops
                // showing the "microphone in use" indicator as well.
                if (this.micEnabled) {
                    this.stopMicrophone().catch(err => {
                        console.warn(err);
                    });
                    mic.classList.remove('active');
                    mic.icon = 'mdi:microphone-off';
                } else {
                    mic.icon = 'mdi:microphone';
                    mic.classList.add('active');
                    // a click is a user gesture: restore sound blocked by autoplay policy
                    this.unmuteAutoplay();
                    this.enableMicrophone().catch(err => {
                        console.warn(err);
                        mic.icon = 'mdi:microphone-off';
                        mic.classList.remove('active');
                    });
                }
            } else if (icon === 'mdi:music-note') {
                this.toggleMusicPicker();
            } else if (icon === 'mdi:music-note-off') {
                this.stopMusic().catch(console.warn);
            } else if (ev.target.className === 'stream') {
                this.nextStream(true);
                ev.target.innerText = this.streamName;
            }
        });

        const spinner = this.querySelector('.spinner');
        video.addEventListener('waiting', () => {
            spinner.style.display = 'block';
        });
        video.addEventListener('playing', () => {
            spinner.style.display = 'none';
        });

        const play = this.querySelector('.play');
        video.addEventListener('play', () => {
            play.style.display = 'none';
        });
        video.addEventListener('pause', () => {
            play.style.display = 'block';
        });

        const volume = this.querySelector('.volume');
        video.addEventListener('loadeddata', () => {
            volume.style.display = this.hasAudio ? 'block' : 'none';
        });
        video.addEventListener('volumechange', () => {
            volume.icon = video.muted ? 'mdi:volume-mute' : 'mdi:volume-high';
        });

        const stream = this.querySelector('.stream');
        stream.style.display = this.config.streams.length > 1 ? 'block' : 'none';
    }

    renderShortcuts() {
        if (!this.config.shortcuts) return;

        const card = this.querySelector('.card');
        card.insertAdjacentHTML('beforebegin', `
            <style>
                .shortcuts {
                    position: absolute;
                    top: 5px;
                    left: 5px;
                }
            </style>
        `);
        card.insertAdjacentHTML('beforeend', '<div class="shortcuts"></div>');

        const shortcuts = this.querySelector('.shortcuts');
        shortcuts.addEventListener('click', ev => {
            const value = this.config.shortcuts[ev.target.dataset.index];
            if (value.more_info !== undefined) {
                const event = new Event('hass-more-info', {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                });
                event.detail = {entityId: value.more_info};
                ev.target.dispatchEvent(event);
            }
            if (value.service !== undefined) {
                const [domain, name] = value.service.split('.');
                this.hass.callService(domain, name, value.service_data || {});
            }
        });

        this.renderTemplate('shortcuts', () => {
            const innerHTML = this.config.shortcuts.map((value, index) => `
                <ha-icon data-index="${index}" icon="${value.icon}" title="${value.name}"></ha-icon>
            `).join('');

            if (shortcuts.innerHTML !== innerHTML) {
                shortcuts.innerHTML = innerHTML;
            }
        });
    }

    /**
     * Add a music button to the control bar.
     *
     * Pressing it opens a list of the media files in the Home Assistant
     * media library; picking one pushes it to this camera's speaker.
     * The media_player that talks to this camera is found by matching the
     * card's stream name against the unique_id of the media_player
     * entities (the upstream webrtc integration uses the go2rtc stream
     * name as the unique_id).
     */
    renderMusic() {
        if (this.config.music === false) return;

        const btn = this.querySelector('.music');
        if (!btn) return;

        const ui = this.querySelector('.ui');
        if (!ui) return;

        const picker = document.createElement('div');
        picker.className = 'music-picker';
        picker.innerHTML = `
            <div class="head">
                <span>播放媒體到這台相機</span>
                <ha-icon icon="mdi:close"></ha-icon>
            </div>
            <div class="list"></div>
        `;
        ui.appendChild(picker);

        // Clicks inside the picker must not reach the control bar handler.
        picker.addEventListener('click', ev => ev.stopPropagation());
        picker.querySelector('ha-icon').addEventListener('click', ev => {
            ev.stopPropagation();
            picker.classList.remove('open');
        });

        this.musicPicker = picker;
        btn.style.display = 'block';
    }

    /** The go2rtc stream name this card shows. */
    get streamKey() {
        const url = this.config.url || '';
        // The card accepts a stream name or an entity id. Only a bare name
        // can be matched against the media_player unique_ids.
        return url.indexOf('://') < 0 && url.indexOf('.') < 0 ? url : '';
    }

    /**
     * Find the media_player that pushes audio to this camera.
     *
     * The media_player can come from the upstream `webrtc` integration or
     * from this fork's own `webrtc_mic` platform - both use the go2rtc
     * stream name as the unique_id, so match on the unique_id and accept
     * either platform.
     */
    async musicPlayer() {
        if (this.config.music_player) return this.config.music_player;
        if (this._musicPlayer) return this._musicPlayer;

        const key = this.streamKey;
        if (!key || !this.hass) return null;

        let entries;
        try {
            entries = await this.hass.callWS({type: 'config/entity_registry/list'});
        } catch (err) {
            console.debug(err);
            return null;
        }

        for (const entry of entries || []) {
            if (entry.unique_id !== key) continue;
            if (entry.platform !== 'webrtc' && entry.platform !== 'webrtc_mic') continue;
            this._musicPlayer = entry.entity_id;
            return this._musicPlayer;
        }
        return null;
    }

    /** Show a message inside the picker, so failures are not silent. */
    musicError(message) {
        const picker = this.musicPicker;
        if (!picker) return;
        const list = picker.querySelector('.list');
        if (list) list.innerHTML = `<div class="empty">${message}</div>`;
        picker.classList.add('open');
    }

    toggleMusicPicker() {
        const picker = this.musicPicker;
        if (!picker) return;
        if (picker.classList.contains('open')) {
            picker.classList.remove('open');
            return;
        }
        this.loadMusicList();
        picker.classList.add('open');
    }

    async loadMusicList() {
        const picker = this.musicPicker;
        if (!picker) return;
        const list = picker.querySelector('.list');
        list.innerHTML = '<div class="empty">載入中…</div>';

        if (!this.hass) return;
        try {
            const res = await this.hass.callWS({
                type: 'media_source/browse_media',
                media_content_id: 'media-source://media_source/local',
            });
            const items = (res && res.children ? res.children : []).filter(i => i.can_play !== false);
            if (!items.length) {
                list.innerHTML = '<div class="empty">沒有可播放的媒體</div>';
                return;
            }
            list.innerHTML = '';
            for (const item of items) {
                const btn = document.createElement('button');
                btn.className = 'item';
                btn.textContent = item.title;
                btn.addEventListener('click', ev => {
                    ev.stopPropagation();
                    this.playMusic(item);
                });
                list.appendChild(btn);
            }
        } catch (err) {
            console.debug(err);
            list.innerHTML = '<div class="empty">取不到媒體清單</div>';
        }
    }

    /**
     * Turn a media library entry into a URL that go2rtc can fetch.
     *
     * Home Assistant signs the media URLs it hands out: the signature is
     * bound to the client that asked for it. go2rtc runs on another host,
     * so a signed URL would make it fail. Resolving the media here and
     * passing the absolute, signed URL on avoids that - the signature is
     * valid for the URL itself, no matter who requests it.
     */
    async musicMediaUrl(item) {
        const contentId = item.media_content_id || '';
        if (!contentId.startsWith('media-source://')) return contentId;

        let resolved;
        try {
            resolved = await this.hass.callWS({
                type: 'media_source/resolve_media',
                media_content_id: contentId,
            });
        } catch (err) {
            console.debug(err);
            return contentId;
        }

        const url = resolved && resolved.url;
        if (!url) return contentId;

        // The resolved URL is relative to the Home Assistant origin.
        const base = (this.hass && this.hass.hassUrl) || document.location.origin;
        try {
            return new URL(url, base).href;
        } catch (err) {
            return url;
        }
    }

    /**
     * Play a track on the camera speaker.
     *
     * The integration ships a `webrtc_mic.play_media` service that needs no
     * configuration at all. When that service is not available yet - an
     * older installation, for example - the card falls back to the
     * media_player entity of the camera.
     */
    async playMusic(item) {
        const stream = this.streamKey;
        if (!stream) {
            this.musicError('這張卡片沒有設定 stream，無法播放');
            return;
        }
        try {
            const mediaUrl = await this.musicMediaUrl(item);
            try {
                await this.hass.callService('webrtc_mic', 'play_media', {
                    stream: stream,
                    media_content_id: mediaUrl,
                });
            } catch (err) {
                if (!this.isMissingService(err)) throw err;
                await this.playMusicWithEntity(item, mediaUrl);
            }
            const btn = this.querySelector('.music');
            if (btn) {
                btn.icon = 'mdi:music-note-off';
                btn.classList.add('playing');
                btn.title = '停止播放';
            }
            if (this.musicPicker) this.musicPicker.classList.remove('open');
        } catch (err) {
            console.warn(err);
            const message = (err && err.message) || String(err);
            this.musicError(`播放失敗：${message}`);
            const btn = this.querySelector('.music');
            if (btn) {
                btn.icon = 'mdi:music-note';
                btn.classList.remove('playing');
                btn.title = '播放媒體到這台相機';
            }
        }
    }

    /** Whether an error means the service is not registered. */
    isMissingService(err) {
        if (!err) return false;
        if (err.code === 'not_found') return true;
        const message = ((err && err.message) || String(err)).toLowerCase();
        return message.includes('not found') || message.includes('找不到服務')
            || message.includes('service not found');
    }

    /** Play through the media_player entity of the camera. */
    async playMusicWithEntity(item, mediaUrl) {
        const player = await this.musicPlayer();
        if (!player) {
            console.warn('No media_player found for stream', this.streamKey);
            this.musicError(`找不到這台相機的對講喇叭（stream: ${this.streamKey || '?'}）`);
            return;
        }
        await this.hass.callService('media_player', 'play_media', {
            entity_id: player,
            media_content_id: mediaUrl,
            media_content_type: 'music',
        });
    }

    async stopMusic() {
        const stream = this.streamKey;
        const btn = this.querySelector('.music');
        if (btn) {
            btn.icon = 'mdi:music-note';
            btn.classList.remove('playing');
            btn.title = '播放媒體到這台相機';
        }
        if (!stream) return;
        try {
            try {
                await this.hass.callService('webrtc_mic', 'stop_media', {stream: stream});
            } catch (err) {
                if (!this.isMissingService(err)) throw err;
                const player = await this.musicPlayer();
                if (player) {
                    await this.hass.callService('media_player', 'media_stop', {entity_id: player});
                }
            }
        } catch (err) {
            console.warn(err);
        }
    }

    /**
     * Show how many people are watching the cameras right now.
     *
     * The endpoint lives on the Home Assistant origin, so the browser can
     * call it without mixed-content problems. It has to be signed like the
     * websocket endpoint - the card asks Home Assistant for a signed path
     * and uses that.
     */
    renderViewers() {
        const box = this.querySelector('.viewers');
        if (!box) return;

        const count = box.querySelector('.count');

        const update = (people, connections) => {
            box.style.display = 'flex';
            box.classList.remove('busy');
            count.innerText = people;
            box.title = people === 1 ? '目前 1 個人在看' : `目前 ${people} 個人在看`;
            if (connections !== people) {
                box.title += `（${connections} 個連線）`;
            }
        };

        const fail = () => {
            box.classList.add('busy');
            count.innerText = '-';
            box.title = '取不到觀看人數';
        };

        if (this.config.viewers === false) {
            box.style.display = 'none';
            return;
        }

        const load = async () => {
            if (!this.hass) return fail();
            try {
                const data = await this.hass.callWS({
                    type: 'auth/sign_path', path: '/api/webrtc_mic/viewers'
                });
                const url = this.hass.hassUrl(data.path);
                const r = await fetch(url, { credentials: 'same-origin' });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const json = await r.json();
                update(json.people | 0, json.connections | 0);
            } catch (e) {
                console.debug(e);
                fail();
            }
        };

        load();
        // Refresh every 20 seconds, but only while the card is on screen.
        clearInterval(this.viewersTID);
        this.viewersTID = setInterval(() => {
            if (!this.isConnected) return;
            const rect = this.getBoundingClientRect();
            const visible = rect.bottom > 0 && rect.top < (window.innerHeight || 0);
            if (visible) load();
        }, 20000);
    }

    renderStyle() {
        if (!this.config.style) return;

        const style = document.createElement('style');
        const card = this.querySelector('.card');
        card.insertAdjacentElement('beforebegin', style);

        this.renderTemplate('style', () => {
            style.innerText = this.config.style;
        });
    }

    renderTemplate(name, renderHTML) {
        const config = this.config[name];
        // support config param as string or as object
        const template = typeof config === 'string' ? config : JSON.stringify(config);
        // check if config param has template
        if (template.indexOf('${') >= 0) {
            const render = () => {
                try {
                    const states = this.hass ? this.hass.states : undefined;
                    this.config[name] = JSON.parse(eval('`' + template + '`'));
                    renderHTML();
                } catch (e) {
                    console.debug(e);
                }
            };
            this.onhass.push(render);
            render();
        } else {
            renderHTML();
        }
    }

    get hasAudio() {
        return (
            (this.video.srcObject && this.video.srcObject.getAudioTracks && this.video.srcObject.getAudioTracks().length) ||
            (this.video.mozHasAudio || this.video.webkitAudioDecodedByteCount) ||
            (this.video.audioTracks && this.video.audioTracks.length)
        );
    }
}

customElements.define('webrtc-mic-camera', WebRTCCamera);

const card = {
    type: 'webrtc-mic-camera',
    name: 'WebRTC Camera with Microphone',
    preview: false,
    description: 'WebRTC camera allows you to view the stream of almost any camera without delay',
};
// Apple iOS 12 doesn't support `||=`
if (window.customCards) window.customCards.push(card);
else window.customCards = [card];

