package com.syncvr.tiling.android

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.view.Surface
import com.syncvr.tiling.MediaClock
import com.syncvr.tiling.TileDecoder
import com.syncvr.tiling.TileId
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicLong

/**
 * Decodes one tile at a time into [surface] (a SurfaceTexture surface owned by the renderer), in
 * step with [clock].
 *
 * A slot is reused for different tiles. When the new tile's codec-specific data matches the running
 * codec (same encoder settings, which tools/tiler.py guarantees) the codec is only flushed; otherwise
 * it is recreated. Starting a tile seeks to the keyframe at or before the clock position and decodes
 * forward without showing frames until it reaches the clock, so a tile appears after at most one
 * keyframe interval of catch-up. Until then the renderer shows the base layer.
 *
 * Frames are released with a render timestamp so they appear when the clock says; frames more than
 * one frame late are dropped. One worker thread per slot.
 *
 * UNVERIFIED on hardware: compiles against the API 25 framework, but timing, catch-up speed and
 * concurrent decoder limits have to be measured on a Go (see [DecoderProbe]).
 */
class MediaCodecTileDecoder(
    override val slot: Int,
    private val surface: Surface,
    /** Absolute path of a tile's video file. */
    private val pathOf: (TileId) -> String,
    private val clock: MediaClock,
    private val frameDurationUs: Long,
    /** Frames are released to the surface this long before they are due (surface latch + compositor). */
    private val leadUs: Long = 30_000,
    private val nanoTime: () -> Long = System::nanoTime,
) : TileDecoder {

    val framesShown = AtomicLong()
    val framesDropped = AtomicLong()

    private val lock = Object()
    private var requested: TileId? = null
    private var released = false

    // Written by the worker, read by the renderer thread.
    @Volatile private var shownTile: TileId? = null
    @Volatile private var visibleFromNanos = Long.MAX_VALUE

    override val tile: TileId? get() = synchronized(lock) { requested }

    override val isShowingFrames: Boolean
        get() = tile != null && shownTile == tile && nanoTime() >= visibleFromNanos

    override fun assign(tile: TileId?) {
        synchronized(lock) {
            if (requested == tile) return
            requested = tile
            shownTile = null
            visibleFromNanos = Long.MAX_VALUE
            lock.notifyAll()
        }
    }

    override fun release() {
        synchronized(lock) { released = true; lock.notifyAll() }
        worker.join(2_000)
    }

    private val worker = Thread({ run() }, "tile-decoder-$slot").also { it.isDaemon = true; it.start() }

    // ---- worker thread state ----
    private var extractor: MediaExtractor? = null
    private var codec: MediaCodec? = null
    private var codecKey: List<ByteArray>? = null
    private var current: TileId? = null
    private var seenEpoch = Int.MIN_VALUE
    private var catchingUp = true
    private var inputDone = false
    private var outputDone = false
    private val info = MediaCodec.BufferInfo()

    private fun run() {
        try {
            while (true) {
                val wanted: TileId?
                synchronized(lock) {
                    if (released) return
                    if (requested == null || (requested == current && outputDone && seenEpoch == clock.epoch)) {
                        if (requested == null && current != null) stopTile()
                        lock.wait(20)
                        if (released) return
                    }
                    wanted = requested
                }
                if (wanted != current) {
                    stopTile()
                    if (wanted != null) startTile(wanted)
                }
                if (current != null) pump()
            }
        } catch (e: Exception) {
            // A failing tile just never shows frames; the base layer stays visible.
            stopTile()
            synchronized(lock) { requested = null }
        } finally {
            stopTile()
            codec?.let { runCatching { it.stop() }; runCatching { it.release() } }
            codec = null
        }
    }

    private fun startTile(tile: TileId) {
        val ex = MediaExtractor()
        ex.setDataSource(pathOf(tile))
        val track = (0 until ex.trackCount).first {
            ex.getTrackFormat(it).getString(MediaFormat.KEY_MIME)!!.startsWith("video/")
        }
        ex.selectTrack(track)
        val format = ex.getTrackFormat(track)
        val key = codecKeyOf(format)
        if (codec != null && key == codecKey) {
            codec!!.flush()
        } else {
            codec?.let { runCatching { it.stop() }; runCatching { it.release() } }
            val c = MediaCodec.createDecoderByType(format.getString(MediaFormat.KEY_MIME)!!)
            c.configure(format, surface, null, 0)
            c.start()
            codec = c
            codecKey = key
        }
        extractor = ex
        current = tile
        seenEpoch = Int.MIN_VALUE // forces the initial seek in pump()
    }

    private fun stopTile() {
        extractor?.release()
        extractor = null
        if (current != null) runCatching { codec?.flush() }
        current = null
        shownTile = null
        visibleFromNanos = Long.MAX_VALUE
    }

    private fun pump() {
        val ex = extractor ?: return
        val c = codec ?: return
        val tile = current ?: return

        if (seenEpoch != clock.epoch) {
            seenEpoch = clock.epoch
            ex.seekTo(clock.positionUs().coerceAtLeast(0), MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
            c.flush()
            inputDone = false
            outputDone = false
            catchingUp = true
            shownTile = null
            visibleFromNanos = Long.MAX_VALUE
        }

        // A paused clock with a frame already on screen: nothing to decode until the position changes.
        if (!clock.isPlaying && !catchingUp && shownTile == tile) {
            Thread.sleep(10)
            return
        }

        if (!inputDone) {
            val i = c.dequeueInputBuffer(0)
            if (i >= 0) {
                val size = ex.readSampleData(c.getInputBuffer(i)!!, 0)
                if (size < 0) {
                    c.queueInputBuffer(i, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    inputDone = true
                } else {
                    c.queueInputBuffer(i, 0, size, ex.sampleTime, 0)
                    ex.advance()
                }
            }
        }

        val out = c.dequeueOutputBuffer(info, 5_000)
        if (out < 0) return
        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
            c.releaseOutputBuffer(out, false)
            outputDone = true
            return
        }
        present(c, out, tile)
    }

    private fun present(c: MediaCodec, index: Int, tile: TileId) {
        val pts = info.presentationTimeUs
        var position = clock.positionUs()

        if (catchingUp) {
            if (pts + frameDurationUs <= position) { // frame ended before the clock: keep decoding
                c.releaseOutputBuffer(index, false)
                return
            }
            catchingUp = false
        }

        if (clock.isPlaying) {
            // Hold the frame until it is nearly due, but notice seeks/loops/reassignment while waiting.
            while (clock.isPlaying && pts - position > leadUs) {
                if (seenEpoch != clock.epoch || tile != tile()) { c.releaseOutputBuffer(index, false); return }
                Thread.sleep(minOf(8L, (pts - position - leadUs) / 1000 + 1))
                position = clock.positionUs()
            }
            if (pts + frameDurationUs < position) { // more than a frame late
                framesDropped.incrementAndGet()
                c.releaseOutputBuffer(index, false)
                return
            }
        }
        val due = nanoTime() + maxOf(0L, pts - position) * 1000
        c.releaseOutputBuffer(index, due)
        framesShown.incrementAndGet()
        if (shownTile != tile) {
            // The renderer may only draw this tile once its first frame has really reached the texture.
            visibleFromNanos = due + frameDurationUs * 1000
            shownTile = tile
        }
    }

    private fun tile(): TileId? = synchronized(lock) { requested }

    private fun codecKeyOf(f: MediaFormat): List<ByteArray> {
        val parts = mutableListOf<ByteArray>()
        parts.add(f.getString(MediaFormat.KEY_MIME)!!.toByteArray())
        parts.add("${f.getInteger(MediaFormat.KEY_WIDTH)}x${f.getInteger(MediaFormat.KEY_HEIGHT)}".toByteArray())
        for (name in listOf("csd-0", "csd-1")) {
            val b: ByteBuffer? = f.getByteBuffer(name)
            parts.add(b?.let { ByteArray(it.remaining()).also { arr -> it.duplicate().get(arr) } } ?: ByteArray(0))
        }
        return parts
    }
}
