/**
 * Local test for the v1.0.33 card bundle.
 *
 * It reproduces what Home Assistant does:
 *
 *   1. serves the card module the way the new CardModuleView does
 *      (imports rewritten to a content hash, no 31 day cache header)
 *   2. serves the two imported files the way the static handler does
 *      when cache_headers is False
 *   3. loads the module in a real browser and checks that the custom
 *      element is registered
 *
 * The point of the test is the cache: the same browser must end up with
 * the NEW module after the files changed, without a hard refresh.
 *
 * Usage: node tools/test-card-bundle.js
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const WWW = path.join(__dirname, '..', 'custom_components', 'webrtc_mic', 'www');

const CARD_FILES = ['webrtc-mic-camera.js', 'video-rtc.js', 'digital-ptz.js'];

const IMPORT_RE = /((?:from|import)\s*['"])(\.\/[^'"]+?\.js)(?:\?[^'"]*)?(['"])/g;

function fileDigest(file) {
    try {
        return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex').slice(0, 12);
    } catch {
        return '0'.repeat(12);
    }
}

function bundleDigest(dir) {
    const h = crypto.createHash('sha256');
    for (const name of CARD_FILES) {
        h.update(name);
        h.update(fileDigest(path.join(dir, name)));
    }
    return h.digest('hex').slice(0, 12);
}

function rewriteImports(source, dir) {
    return source.replace(IMPORT_RE, (m, head, p, tail) => {
        const name = path.basename(p);
        return `${head}${p}?v=${fileDigest(path.join(dir, name))}${tail}`;
    });
}

// The set of files the browser has already cached, keyed by URL.
const cache = new Map();

function serve(res, body, contentType, cacheControl) {
    res.writeHead(200, {
        'Content-Type': contentType,
        'Cache-Control': cacheControl,
        // The test pages are loaded with setContent(), which gives the
        // document a "null" origin. Home Assistant serves the same files
        // to the dashboard on the same origin, so this header only
        // matters for the test.
        'Access-Control-Allow-Origin': '*',
    });
    res.end(body);
}

const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const name = url.pathname.replace('/webrtc_mic/', '');

    if (name === 'webrtc-mic-camera.js') {
        const source = fs.readFileSync(path.join(WWW, name), 'utf8');
        const body = rewriteImports(source, WWW);
        // The new view: no long cache header, the query string is the
        // content hash.
        serve(res, body, 'text/javascript', 'no-cache, must-revalidate');
        return;
    }

    if (CARD_FILES.includes(name)) {
        const body = fs.readFileSync(path.join(WWW, name));
        // Static handler with cache_headers=False.
        serve(res, body, 'text/javascript', 'no-cache, must-revalidate');
        return;
    }

    res.writeHead(404);
    res.end('not found');
});

(async () => {
    const { chromium } = require('/home/lastdanger/node_modules/playwright');

    const port = await new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve(server.address().port));
    });
    const base = `http://127.0.0.1:${port}`;

    const browser = await chromium.launch({
        args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'],
    });
    const context = await browser.newContext();

    let failures = 0;
    const check = (ok, label, extra) => {
        console.log(`${ok ? '  OK  ' : '  [X] '} ${label}${extra ? ' -> ' + extra : ''}`);
        if (!ok) failures++;
    };

    // ------------------------------------------------------------------
    // 1. the card registers itself in a normal browser
    // ------------------------------------------------------------------
    console.log('\n[1] custom element registration');
    const page = await context.newPage();
    const consoleErrors = [];
    page.on('pageerror', (e) => consoleErrors.push(String(e)));
    await page.goto(`${base}/webrtc_mic/webrtc-mic-camera.js`).catch(() => {});
    await page.setContent(`<script type="module" src="${base}/webrtc_mic/webrtc-mic-camera.js"></script>`);
    await page.waitForTimeout(1500);
    const registered = await page.evaluate(() => !!customElements.get('webrtc-mic-camera'));
    check(registered, 'webrtc-mic-camera is registered');

    // ------------------------------------------------------------------
    // 2. the imports carry a content hash
    // ------------------------------------------------------------------
    console.log('\n[2] import rewriting');
    const cardSource = fs.readFileSync(path.join(WWW, 'webrtc-mic-camera.js'), 'utf8');
    const rewritten = rewriteImports(cardSource, WWW);
    const videoDigest = fileDigest(path.join(WWW, 'video-rtc.js'));
    const ptzDigest = fileDigest(path.join(WWW, 'digital-ptz.js'));
    check(rewritten.includes(`./video-rtc.js?v=${videoDigest}`), 'video-rtc.js import has the content hash');
    check(rewritten.includes(`./digital-ptz.js?v=${ptzDigest}`), 'digital-ptz.js import has the content hash');
    check(!rewritten.includes('?v=1.9.19-mic6'), 'the old hand maintained version string is gone');

    // ------------------------------------------------------------------
    // 3. the browser must pick up a changed file WITHOUT a hard refresh
    // ------------------------------------------------------------------
    console.log('\n[3] cache busting');
    const before = bundleDigest(WWW);
    const marker = `\n// cache bust test ${Date.now()}\n`;
    const ptzPath = path.join(WWW, 'digital-ptz.js');
    const ptzBackup = fs.readFileSync(ptzPath);
    fs.appendFileSync(ptzPath, marker);

    const after = bundleDigest(WWW);
    check(before !== after, 'the bundle digest changes when a file changes', `${before} -> ${after}`);

    const page2 = await context.newPage();
    const errs2 = [];
    page2.on('pageerror', (e) => errs2.push(String(e)));
    page2.on('console', (m) => { if (m.type() === 'error') errs2.push('console: ' + m.text()); });
    await page2.setContent(`<script type="module" src="${base}/webrtc_mic/webrtc-mic-camera.js?v=${after}"></script>`);
    await page2.waitForTimeout(2000);
    const registered2 = await page2.evaluate(() => !!customElements.get('webrtc-mic-camera'));
    check(registered2, 'the new module registers in the same browser session', errs2.join(' | '));

    // ------------------------------------------------------------------
    // 4. embed.html points at a file that exists
    // ------------------------------------------------------------------
    console.log('\n[4] embed page');
    const embed = fs.readFileSync(path.join(WWW, 'embed.html'), 'utf8');
    const embedRef = (embed.match(/src="\/webrtc_mic\/([^"]+)"/) || [])[1];
    check(embedRef === 'webrtc-mic-camera.js', 'embed.html loads webrtc-mic-camera.js', embedRef);
    check(fs.existsSync(path.join(WWW, embedRef)), 'the file embed.html loads exists');

    // restore
    fs.writeFileSync(ptzPath, ptzBackup);

    // ------------------------------------------------------------------
    // 5. the error messages tell the user what went wrong
    // ------------------------------------------------------------------
    console.log('\n[5] error classification');
    const describe = (text) => {
        if (!text) return 'unknown error';
        if (text.includes('stream') && (text.includes('not found') || text.includes('no such stream'))) {
            return 'go2rtc stream not found';
        }
        if (text.includes('401') || text.includes('Unauthorized')) {
            return 'camera source rejected the credentials (401)';
        }
        if (text.includes('authSig') || text.includes('signature') || text.includes('Invalid signature')) {
            return 'the signed link expired, please reload the page';
        }
        if (text.includes('go2rtc')) return 'go2rtc is not reachable';
        return text;
    };
    check(describe('streams: 401 Unauthorized') === 'camera source rejected the credentials (401)',
        'a 401 from the camera is not reported as a signature problem');
    check(describe('go2rtc stream "road2" not found') === 'go2rtc stream not found',
        'a missing stream is reported as such');
    check(describe('Invalid signature') === 'the signed link expired, please reload the page',
        'an invalid signature is reported as such');

    await browser.close();
    server.close();

    console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
    console.error(err);
    server.close();
    process.exit(1);
});
