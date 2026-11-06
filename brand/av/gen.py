import subprocess, pathlib

HEAD = '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:transparent}.c{width:512px;height:512px;overflow:hidden}</style></head><body><div class="c">'
TAIL = '</div></body></html>'

A = '''
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
<defs>
<radialGradient id="bg" cx="0.5" cy="0.42" r="0.75">
<stop offset="0" stop-color="#102A47"/><stop offset="1" stop-color="#050D18"/></radialGradient>
<radialGradient id="sweep" cx="0.5" cy="0.5" r="0.5">
<stop offset="0" stop-color="#36F2A6" stop-opacity="0.55"/>
<stop offset="1" stop-color="#36F2A6" stop-opacity="0"/></radialGradient>
<filter id="g"><feGaussianBlur stdDeviation="7" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="512" height="512" fill="url(#bg)"/>
<g fill="none" stroke="#36F2A6" stroke-opacity="0.22" stroke-width="3">
<circle cx="256" cy="256" r="72"/><circle cx="256" cy="256" r="130"/><circle cx="256" cy="256" r="188"/>
</g>
<g stroke="#36F2A6" stroke-opacity="0.18" stroke-width="3">
<path d="M256 68 L256 444"/><path d="M68 256 L444 256"/>
</g>
<path d="M256 256 L256 62 A194 194 0 0 1 393 119 Z" fill="url(#sweep)"/>
<g filter="url(#g)">
<circle cx="330" cy="168" r="17" fill="#8BFFD0"/>
</g>
<circle cx="168" cy="318" r="10" fill="#36F2A6" fill-opacity="0.65"/>
<circle cx="322" cy="336" r="8" fill="#36F2A6" fill-opacity="0.45"/>
<circle cx="256" cy="256" r="12" fill="#EAFFF6"/>
</svg>'''

B = '''
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">
<stop offset="0" stop-color="#20242B"/><stop offset="1" stop-color="#0E1013"/></linearGradient>
<linearGradient id="am" gradientUnits="userSpaceOnUse" x1="106" y1="106" x2="406" y2="406">
<stop offset="0" stop-color="#FFD166"/><stop offset="1" stop-color="#FF9F1C"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<circle cx="256" cy="256" r="150" fill="none" stroke="url(#am)" stroke-width="26"/>
<g stroke="url(#am)" stroke-width="26" stroke-linecap="round">
<path d="M256 48 L256 112"/><path d="M256 400 L256 464"/>
<path d="M48 256 L112 256"/><path d="M400 256 L464 256"/>
</g>
<circle cx="256" cy="256" r="52" fill="url(#am)"/>
<circle cx="256" cy="256" r="52" fill="none" stroke="#0E1013" stroke-width="10" stroke-opacity="0.25"/>
</svg>'''

C = '''
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#3B2BD6"/><stop offset="1" stop-color="#1A1060"/></linearGradient>
<linearGradient id="mg" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#FF5B5B"/><stop offset="1" stop-color="#D32B2B"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<g transform="rotate(-20 256 272)">
<path d="M146 300 A110 110 0 0 1 366 300 L366 330 L292 330 L292 300 A36 36 0 0 0 220 300 L220 330 L146 330 Z" fill="url(#mg)"/>
<rect x="146" y="330" width="74" height="54" rx="8" fill="#F2F5FF"/>
<rect x="292" y="330" width="74" height="54" rx="8" fill="#F2F5FF"/>
</g>
<g fill="#FFD166">
<circle cx="256" cy="120" r="19"/>
<circle cx="160" cy="166" r="13" fill-opacity="0.8"/>
<circle cx="352" cy="166" r="13" fill-opacity="0.8"/>
</g>
</svg>'''

D = '''
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#2D7BFF"/><stop offset="1" stop-color="#15256B"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<circle cx="256" cy="256" r="196" fill="none" stroke="#FFFFFF" stroke-opacity="0.18" stroke-width="6"/>
<text x="256" y="258" text-anchor="middle" dominant-baseline="central"
 font-family="Inter Display, Inter, sans-serif" font-weight="800" font-size="210"
 letter-spacing="-10" fill="#FFFFFF">LH</text>
<circle cx="256" cy="372" r="13" fill="#5BE2A0"/>
</svg>'''

E = '''
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#14C7A0"/><stop offset="1" stop-color="#0A6B6B"/></linearGradient>
<linearGradient id="hd" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#DCEFEC"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<g stroke="#FFFFFF" stroke-width="14" stroke-linecap="round" fill="none">
<path d="M256 132 L256 96"/></g>
<circle cx="256" cy="84" r="20" fill="#FFFFFF"/>
<rect x="134" y="140" width="244" height="208" rx="56" fill="url(#hd)"/>
<g fill="#0C3A3A"><circle cx="208" cy="236" r="24"/><circle cx="304" cy="236" r="24"/></g>
<path d="M212 292 Q256 322 300 292" stroke="#0C3A3A" stroke-width="16" fill="none" stroke-linecap="round"/>
<rect x="186" y="364" width="140" height="26" rx="13" fill="#FFFFFF" fill-opacity="0.75"/>
</svg>'''

for name, svg in (("1-radar", A), ("2-pricel", B), ("3-magnit", C), ("4-monogram", D), ("5-bot", E)):
    pathlib.Path(f"{name}.html").write_text(HEAD + svg + TAIL, encoding="utf-8")
    subprocess.run(["/opt/pw-browsers/chromium", "--headless", "--no-sandbox", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--window-size=512,512",
                    f"--screenshot={name}.png", f"file:///home/user/search/brand/av/{name}.html"],
                   capture_output=True)
    print(name, pathlib.Path(f"{name}.png").stat().st_size)
