package com.syncvr.tiling

/**
 * The playback position tiles must follow, normally the base layer's player (it owns the audio).
 * [epoch] changes whenever the position jumps (seek, loop, new video) so tile decoders re-seek
 * instead of trying to catch up.
 */
interface MediaClock {
    fun positionUs(): Long
    val epoch: Int
    val isPlaying: Boolean
}

/**
 * One hardware decoder instance (a "slot") that can be pointed at different tiles. Implementations
 * run their own thread; every method here only hands work over and returns quickly.
 */
interface TileDecoder {
    val slot: Int

    /** Tile currently assigned, or null when idle. */
    val tile: TileId?

    /**
     * True once the assigned tile has shown a frame for the clock's current position, so the
     * renderer can draw it over the base layer. Goes false again on a seek until it catches up.
     */
    val isShowingFrames: Boolean

    /** Starts decoding [tile], or goes idle with null. */
    fun assign(tile: TileId?)

    fun release()
}

/**
 * Owns the decoder slots, applies scheduler decisions and tells the renderer which slot shows
 * which tile.
 */
class TilePool(private val decoders: List<TileDecoder>, private val scheduler: TileScheduler) {
    val budget get() = decoders.size

    val activeTiles: Set<TileId> get() = decoders.mapNotNull { it.tile }.toSet()

    /** Slot index per tile that can be drawn now; everything else falls back to the base layer. */
    fun drawable(): Map<TileId, Int> =
        decoders.filter { it.isShowingFrames }.mapNotNull { d -> d.tile?.let { it to d.slot } }.toMap()

    /** Call a few times per second (or when the view tile set changes) with the ranked wanted tiles. */
    fun update(nowUs: Long, wanted: List<TileId>): TileDecision {
        val decision = scheduler.update(nowUs, wanted, activeTiles)
        for (t in decision.stop) decoders.first { it.tile == t }.assign(null)
        for (t in decision.start) decoders.first { it.tile == null }.assign(t)
        return decision
    }

    fun release() = decoders.forEach { it.release() }
}
