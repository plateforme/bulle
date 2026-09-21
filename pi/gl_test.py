"""Essai : shader plein écran (formes à distance signée) via pygame + OpenGL ES 2 sur KMS — mesure des images/s."""
import os, sys, time
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
if os.environ.get("SDL_VIDEODRIVER") == "kmsdrm":
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
import pygame
from OpenGL import GL as gl

VS = """
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
"""
FS = """
#ifdef GL_ES
precision mediump float;
#endif
uniform vec2 u_res;
uniform float u_t;
float sdRoundBox(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r; }
void main() {
    vec2 p = vec2(gl_FragCoord.x, u_res.y - gl_FragCoord.y);
    float K = u_res.x / 640.0;
    vec3 col = vec3(0.0);
    for (int i = 0; i < 2; i++) {
        vec2 c = vec2(u_res.x * 0.5 + (i == 0 ? -106.0 : 106.0) * K, u_res.y * 0.43);
        float d = sdRoundBox(p - c, vec2(38.0, 51.0) * K, 38.0 * K);
        float eye = clamp(0.5 - d, 0.0, 1.0);
        float glow = exp(-max(d, 0.0) / (7.0 * K)) * 0.3;
        vec2 pc = c + vec2(sin(u_t) * 14.0, cos(u_t * 0.7) * 10.0) * K;
        float pup = clamp(0.5 - (length(p - pc) - 22.0 * K), 0.0, 1.0);
        col += vec3(0.95, 0.93, 0.9) * (eye * (1.0 - pup) + glow * (1.0 - eye));
    }
    gl_FragColor = vec4(col, 1.0);
}
"""


def shader(kind, src):
    s = gl.glCreateShader(kind); gl.glShaderSource(s, src); gl.glCompileShader(s)
    if not gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS): raise RuntimeError(gl.glGetShaderInfoLog(s))
    return s


def main():
    pygame.display.init()
    if os.environ.get("SDL_VIDEODRIVER") == "kmsdrm":
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 2)
    screen = pygame.display.set_mode((0, 0), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
    W, H = screen.get_size()
    print("écran", W, H, "|", gl.glGetString(gl.GL_RENDERER).decode(), "|", gl.glGetString(gl.GL_VERSION).decode(), flush=True)
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, shader(gl.GL_VERTEX_SHADER, VS)); gl.glAttachShader(prog, shader(gl.GL_FRAGMENT_SHADER, FS))
    gl.glBindAttribLocation(prog, 0, "a_pos"); gl.glLinkProgram(prog); gl.glUseProgram(prog)
    import array
    quad = array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1])
    vbo = gl.glGenBuffers(1); gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.tobytes(), gl.GL_STATIC_DRAW)
    gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
    ures, ut = gl.glGetUniformLocation(prog, "u_res"), gl.glGetUniformLocation(prog, "u_t")
    gl.glViewport(0, 0, W, H); gl.glUniform2f(ures, W, H)
    t0, n, dur = time.time(), 0, float(sys.argv[1]) if len(sys.argv) > 1 else 10
    while time.time() - t0 < dur:
        pygame.event.pump()
        gl.glUniform1f(ut, time.time() - t0)
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        pygame.display.flip(); n += 1
    print(f"{n / (time.time() - t0):.1f} ips en {W}x{H}", flush=True)


if __name__ == "__main__":
    main()
