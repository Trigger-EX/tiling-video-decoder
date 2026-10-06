package com.syncvr.tiling.android

import android.graphics.SurfaceTexture
import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import android.view.Surface

/**
 * How many hardware video decoders of a given size the device lets us run at once. This number
 * (not tile granularity) caps how finely a video can be tiled, so measure it on the headset before
 * picking `--cols/--rows`; ship the result in the diagnostics log next to the existing decoder limits.
 */
object DecoderProbe {
    data class Result(
        val codecName: String?,
        /** Limit the codec reports itself (API 23+); often optimistic. */
        val reportedMaxInstances: Int,
        /** Decoders we actually managed to configure and start at the same time. */
        val measuredInstances: Int,
    )

    fun measure(mime: String, width: Int, height: Int, maxTry: Int = 24): Result {
        val name = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
            .firstOrNull { !it.isEncoder && it.supportedTypes.any { t -> t.equals(mime, true) } }
        val reported = name?.getCapabilitiesForType(mime)?.maxSupportedInstances ?: -1

        val codecs = ArrayList<MediaCodec>()
        val textures = ArrayList<SurfaceTexture>()
        val surfaces = ArrayList<Surface>()
        try {
            repeat(maxTry) {
                val tex = SurfaceTexture(0)
                val surface = Surface(tex)
                textures.add(tex)
                surfaces.add(surface)
                try {
                    val c = MediaCodec.createDecoderByType(mime)
                    codecs.add(c)
                    c.configure(MediaFormat.createVideoFormat(mime, width, height), surface, null, 0)
                    c.start()
                } catch (e: Exception) {
                    return Result(name?.name, reported, codecs.size - 1)
                }
            }
            return Result(name?.name, reported, codecs.size)
        } finally {
            codecs.forEach { runCatching { it.stop() }; runCatching { it.release() } }
            surfaces.forEach { it.release() }
            textures.forEach { it.release() }
        }
    }
}
