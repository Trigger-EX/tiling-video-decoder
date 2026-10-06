package com.syncvr.tiling

/** What to change so the decoders match the wanted tiles. Apply [stop] before [start]. */
data class TileDecision(val start: List<TileId>, val stop: List<TileId>)

/**
 * Chooses which tiles hold a decoder. At most [budget] tiles are active (hardware decoder
 * instances are scarce on the Go). Wanted tiles are taken in rank order; a tile that just left the
 * wanted set keeps its decoder for [lingerUs] while capacity remains, so jittering head movement
 * at a tile border doesn't start and stop the same decoder over and over (each start costs up to
 * one keyframe interval of catch-up before the tile shows).
 */
class TileScheduler(private val budget: Int, private val lingerUs: Long = 1_500_000) {
    init {
        require(budget >= 1)
    }

    private val lastWanted = HashMap<TileId, Long>()

    /**
     * @param wanted visible tiles, most important first
     * @param active tiles that currently hold a decoder
     */
    fun update(nowUs: Long, wanted: List<TileId>, active: Set<TileId>): TileDecision {
        for (t in wanted) lastWanted[t] = nowUs
        val keep = LinkedHashSet<TileId>(wanted.take(budget))
        // Fill spare capacity with lingering active tiles, most recently wanted first.
        active.filter { it !in keep && nowUs - (lastWanted[it] ?: Long.MIN_VALUE / 2) <= lingerUs }
            .sortedByDescending { lastWanted[it] ?: Long.MIN_VALUE }
            .forEach { if (keep.size < budget) keep.add(it) }
        lastWanted.keys.retainAll { nowUs - (lastWanted[it] ?: 0) <= lingerUs || it in active }
        return TileDecision(
            start = keep.filter { it !in active },
            stop = active.filter { it !in keep },
        )
    }
}
