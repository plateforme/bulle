"""Diagnostic du suivi : photo couleur Kinect + profondeur, avec ce que le suivi prend pour une personne entouré en rouge."""
import ctypes, sys, time

import numpy as np
import pygame

import tracker as tk

tk.lib.freenect_sync_get_video.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int, ctypes.c_int]


def rgb():
    ptr, ts = ctypes.c_void_p(), ctypes.c_uint32()
    if tk.lib.freenect_sync_get_video(ctypes.byref(ptr), ctypes.byref(ts), 0, 0) != 0:
        return None
    return np.frombuffer((ctypes.c_uint8 * (640 * 480 * 3)).from_address(ptr.value), np.uint8).reshape(480, 640, 3).copy()


def main(out, learn=10.0):
    bg = prev = None
    flick = np.zeros((tk.GH, tk.GW), np.float32)
    hits = np.zeros((tk.GH, tk.GW), np.int32)
    t0, n = time.time(), 0
    while time.time() - t0 < learn:
        g = tk.depth_grid()
        if g is None: continue
        known = g > 0
        if bg is None: bg = g.copy()
        stable = known if prev is None else known & (prev > 0) & (np.abs(g - prev) < 0.08)
        if prev is not None:
            flick = 0.97 * flick + 0.03 * ((known != (prev > 0)) | (known & (prev > 0) & (np.abs(g - prev) > 0.15)))
        bg = np.where(stable & (g > bg), g, bg); prev = g
        fg = known & (bg - g > 0.25) & (g > 0.6) & (g < 4.5)
        fg = fg & (np.roll(fg, 1, 0) | np.roll(fg, -1, 0)) & (np.roll(fg, 1, 1) | np.roll(fg, -1, 1))
        fg[:, :4] = False; fg[:, -5:] = False; fg &= flick < 0.25
        hits += fg; n += 1
        time.sleep(0.1)
    p = tk.find_person(fg, g)
    img = rgb()
    pygame.init()
    W, H = 640, 480
    sheet = pygame.Surface((W * 2, H + 40)); sheet.fill((20, 20, 20))
    if img is not None:
        sheet.blit(pygame.surfarray.make_surface(img.swapaxes(0, 1)), (0, 0))
    d = np.clip(g / 4.5, 0, 1)
    dep = (np.stack([1 - d, 1 - d, 1 - d], -1) * 255 * (g > 0)[..., None]).astype(np.uint8)
    dep = np.kron(dep, np.ones((8, 8, 1), np.uint8))
    sheet.blit(pygame.surfarray.make_surface(dep.swapaxes(0, 1)), (W, 0))
    # cases souvent prises pour une personne pendant l'apprentissage (rouge) et silhouette retenue (cadre)
    freq = hits / max(1, n)
    ov = pygame.Surface((W, H), pygame.SRCALPHA)
    for y, x in zip(*np.where(freq > 0.2)):
        ov.fill((255, 40, 40, int(90 + 140 * freq[y, x])), pygame.Rect(x * 8, y * 8, 8, 8))
    for y, x in zip(*np.where(flick >= 0.25)):
        ov.fill((60, 140, 255, 120), pygame.Rect(x * 8, y * 8, 8, 8))
    sheet.blit(ov, (0, 0)); sheet.blit(ov, (W, 0))
    f = pygame.font.Font(None, 24)
    msg = "rouge = pris pour une personne ; bleu = cases qui scintillent (ignorees)"
    if p:
        hx, top, dist = p
        ax = ((hx + 0.5) / tk.GW - 0.5) * tk.FOV_X
        msg += f"  |  retenu : angle {ax:+.0f} deg, {dist:.1f} m"
        pygame.draw.circle(sheet, (255, 220, 0), (int(hx * 8), top * 8), 14, 3)
        pygame.draw.circle(sheet, (255, 220, 0), (W + int(hx * 8), top * 8), 14, 3)
    else:
        msg += "  |  personne retenue : aucune"
    sheet.blit(f.render(msg, True, (230, 230, 230)), (10, H + 10))
    pygame.image.save(sheet, out)
    print("photo :", out, "|", msg)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fantome.png")
