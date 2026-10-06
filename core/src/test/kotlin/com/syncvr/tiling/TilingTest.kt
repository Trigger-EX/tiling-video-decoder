package com.syncvr.tiling

import kotlin.math.PI
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

private val grid8x4 = Grid(cols = 8, rows = 4, tileWidth = 1024, tileHeight = 1024, pad = 16) // 8192x4096
private val goFov = Fov.degrees(100.0, 100.0)

class ManifestTest {
    private fun manifest(version: Int = 1, stereo: String = "none", tiles: Int = 8) = """
        {"version":$version,"projection":"equirect","stereo":"$stereo","width":1280,"height":640,"fps":30.0,
         "durationSeconds":4.0,"codec":"h264","gopFrames":30,"gopSeconds":1.0,
         "grid":{"cols":4,"rows":2,"tileWidth":320,"tileHeight":320,"pad":16},
         "base":{"file":"base.mp4","width":320,"height":160},
         "tiles":[${(0 until tiles).joinToString(",") { """{"row":${it / 4},"col":${it % 4},"file":"tiles/t_${it / 4}_${it % 4}.mp4"}""" }}]}
    """.trimIndent()

    @Test fun parsesTilerOutput() {
        val t = Tileset.parse(manifest())
        assertEquals(Grid(4, 2, 320, 320, 16), t.grid)
        assertEquals("tiles/t_1_2.mp4", t.fileOf(TileId(1, 2)))
        assertEquals(352, t.grid.codedWidth)
        assertEquals("base.mp4", t.baseFile)
    }

    @Test fun rejectsBadManifests() {
        assertFailsWith<IllegalArgumentException> { Tileset.parse(manifest(version = 2)) }
        assertFailsWith<IllegalArgumentException> { Tileset.parse(manifest(stereo = "tb")) }
        assertFailsWith<IllegalArgumentException> { Tileset.parse(manifest(tiles = 7)) }
    }
}

class GeometryTest {
    private val g = TileGeometry(grid8x4)

    @Test fun forwardIsTheCentreSeam() {
        // Straight ahead (-Z) is u = 0.5: the boundary between columns 3 and 4, on the equator.
        assertEquals(TileId(1, 4), g.tileAt(doubleArrayOf(0.0, 0.0001, -1.0)).let { it.copy(row = 1) })
        assertEquals(4, g.tileAt(doubleArrayOf(0.001, 0.0, -1.0)).col)
        assertEquals(3, g.tileAt(doubleArrayOf(-0.001, 0.0, -1.0)).col)
    }

    @Test fun rightIsPlusXAndUpIsRowZero() {
        assertEquals(5, g.tileAt(Sphere.dir(Math.toRadians(70.0), 0.0)).col) // 70deg right: u = 0.69 -> col 5 of 8
        assertEquals(0, g.tileAt(Sphere.dir(0.1, Math.toRadians(80.0))).row)
        assertEquals(3, g.tileAt(Sphere.dir(0.1, Math.toRadians(-80.0))).row)
    }

    @Test fun wrapsBehindTheViewer() {
        assertEquals(7, g.tileAt(Sphere.dir(PI - 0.01, 0.1)).col)
        assertEquals(0, g.tileAt(Sphere.dir(-PI + 0.01, 0.1)).col)
    }

    @Test fun tileCentreLiesInsideItsOwnTile() {
        for (t in grid8x4.all()) assertEquals(t, g.tileAt(g.centerDir(t)))
    }

    @Test fun innerRectExcludesPadding() {
        val r = g.innerRect()
        assertEquals(16f / 1056f, r[0]); assertEquals(1040f / 1056f, r[2])
    }

    @Test fun yawTurnsLeftPitchLooksUp() {
        val left = Orientation.fromYawPitch(Math.toRadians(90.0), 0.0).rotate(doubleArrayOf(0.0, 0.0, -1.0))
        assertTrue(left[0] < -0.99, "yaw +90 should look along -X, was ${left.toList()}")
        val up = Orientation.fromYawPitch(0.0, Math.toRadians(90.0)).rotate(doubleArrayOf(0.0, 0.0, -1.0))
        assertTrue(up[1] > 0.99)
        val both = Orientation.fromYawPitch(Math.toRadians(90.0), Math.toRadians(45.0)).rotate(doubleArrayOf(0.0, 0.0, -1.0))
        assertEquals(Math.toRadians(45.0), Sphere.elevation(both[1]), 1e-9)
        assertEquals(Math.toRadians(-90.0), Sphere.azimuth(both[0], both[2]), 1e-9)
    }
}

class VisibilityTest {
    private val planner = VisibilityPlanner(TileGeometry(grid8x4))

    @Test fun lookingForwardSeesTheCentreColumns() {
        val tiles = planner.plan(listOf(Orientation.IDENTITY), goFov).map { it.tile }
        assertTrue(setOf(3, 4).all { c -> tiles.any { it.col == c } })   // both sides of the forward seam
        assertEquals(setOf(0, 1, 2, 3), tiles.map { it.row }.toSet())   // +-50deg reaches past the 45deg rows
        assertEquals(12, tiles.size)                                    // 3 columns x 4 rows (corner rays are wider)
    }

    @Test fun ranksCentralTilesFirst() {
        val wanted = planner.plan(listOf(Orientation.IDENTITY), goFov)
        assertEquals(wanted.sortedBy { it.angleRad }, wanted)
        assertTrue(wanted.first().tile.col in 3..4 && wanted.first().tile.row in 1..2)
    }

    @Test fun lookingBackWrapsAroundTheSeam() {
        val cols = planner.plan(listOf(Orientation.fromYawPitch(PI, 0.0)), goFov).map { it.tile.col }.toSet()
        assertTrue(0 in cols && 7 in cols, "expected both sides of the wrap, got $cols")
    }

    @Test fun lookingStraightUpOnlySeesTopRows() {
        val rows = planner.plan(listOf(Orientation.fromYawPitch(0.0, Math.toRadians(89.0))), goFov).map { it.tile.row }.toSet()
        assertTrue(3 !in rows)
        assertTrue(0 in rows)
    }

    @Test fun marginAndPredictionAddTilesAfterTheCurrentOnes() {
        val now = Orientation.IDENTITY
        val predicted = Orientation.fromYawPitch(Math.toRadians(-40.0), 0.0)
        val base = planner.plan(listOf(now), goFov).map { it.tile }
        val withPrediction = planner.plan(listOf(now, predicted), goFov).map { it.tile }
        assertTrue(withPrediction.size > base.size)
        assertEquals(base.toSet(), withPrediction.take(base.size).toSet(), "current-pose tiles must rank first")
        assertTrue(planner.plan(listOf(now), goFov, margin = Math.toRadians(20.0)).size > base.size)
    }

    @Test fun decodesAFractionOfTheFullFrame() {
        val n = planner.plan(listOf(Orientation.IDENTITY), goFov).size
        val frac = DecodeCost.fractionOfFullFrame(grid8x4, n, 1280, 640)
        println("visible tiles $n of 32 -> ${"%.0f".format(frac * 100)}% of the full-frame pixels (incl. base layer)")
        assertTrue(frac < 0.6, "was $frac")
    }
}

class SchedulerTest {
    private fun t(c: Int) = TileId(0, c)

    @Test fun startsWantedAndNeverExceedsBudget() {
        val s = TileScheduler(budget = 3)
        val d = s.update(0, listOf(t(0), t(1), t(2), t(3), t(4)), emptySet())
        assertEquals(listOf(t(0), t(1), t(2)), d.start)
        assertTrue(d.stop.isEmpty())
    }

    @Test fun keepsALeavingTileUntilLingerExpires() {
        val s = TileScheduler(budget = 4, lingerUs = 1_000)
        s.update(0, listOf(t(0), t(1)), emptySet())
        val still = s.update(500, listOf(t(1)), setOf(t(0), t(1)))
        assertTrue(still.stop.isEmpty() && still.start.isEmpty())
        val gone = s.update(2_000, listOf(t(1)), setOf(t(0), t(1)))
        assertEquals(listOf(t(0)), gone.stop)
    }

    @Test fun wantedTileEvictsLingeringOneWhenFull() {
        val s = TileScheduler(budget = 2, lingerUs = 10_000)
        s.update(0, listOf(t(0), t(1)), emptySet())
        val d = s.update(100, listOf(t(1), t(2)), setOf(t(0), t(1)))
        assertEquals(listOf(t(0)), d.stop)
        assertEquals(listOf(t(2)), d.start)
    }

    @Test fun lowerRankedWantedTileIsEvictedBeforeHigherRanked() {
        val s = TileScheduler(budget = 2)
        s.update(0, listOf(t(0), t(1)), emptySet())
        val d = s.update(10, listOf(t(2), t(1), t(0)), setOf(t(0), t(1)))   // t(0) now ranks last
        assertEquals(listOf(t(0)), d.stop)
        assertEquals(listOf(t(2)), d.start)
    }
}

class PoolTest {
    private class Fake(override val slot: Int) : TileDecoder {
        override var tile: TileId? = null
        override var isShowingFrames = false
        override fun assign(tile: TileId?) { this.tile = tile; isShowingFrames = false }
        override fun release() {}
    }

    @Test fun assignsFreeSlotsAndReportsOnlyDrawableTiles() {
        val fakes = List(3) { Fake(it) }
        val pool = TilePool(fakes, TileScheduler(3))
        pool.update(0, listOf(TileId(0, 0), TileId(0, 1)))
        assertEquals(setOf(TileId(0, 0), TileId(0, 1)), pool.activeTiles)
        assertTrue(pool.drawable().isEmpty(), "no frames yet: base layer only")
        fakes.first { it.tile == TileId(0, 1) }.isShowingFrames = true
        assertEquals(mapOf(TileId(0, 1) to fakes.first { it.tile == TileId(0, 1) }.slot), pool.drawable())
    }

    @Test fun reassignsASlotWhenTheViewMoves() {
        val fakes = List(2) { Fake(it) }
        val pool = TilePool(fakes, TileScheduler(2, lingerUs = 0))
        pool.update(0, listOf(TileId(0, 0), TileId(0, 1)))
        pool.update(1_000_000, listOf(TileId(0, 5), TileId(0, 1)))
        assertEquals(setOf(TileId(0, 5), TileId(0, 1)), pool.activeTiles)
    }
}

class GridSizingTest {
    /** Prints how grid choice trades decode cost against the number of decoders needed. */
    @Test fun printSizingTable() {
        val fov = Fov.degrees(100.0, 100.0)
        println("grid   | tile deg    | tiles visible avg/max | decoded pixels vs full frame (padding + base layer included)")
        for ((cols, rows) in listOf(6 to 3, 8 to 4, 12 to 6, 16 to 8, 24 to 12)) {
            val g = Grid(cols, rows, 7680 / cols, 3840 / rows, 16)
            val planner = VisibilityPlanner(TileGeometry(g))
            var sum = 0; var count = 0; var max = 0
            for (yawDeg in 0 until 360 step 5) for (pitchDeg in -45..45 step 15) {
                val view = Orientation.fromYawPitch(Math.toRadians(yawDeg.toDouble()), Math.toRadians(pitchDeg.toDouble()))
                val n = planner.plan(listOf(view), fov).size
                sum += n; count++; max = maxOf(max, n)
            }
            val avg = sum.toDouble() / count
            val frac = (avg * g.codedWidth * g.codedHeight + 1280.0 * 640) / (g.width.toDouble() * g.height)
            println("%2dx%-2d  | %4.1f x %4.1f | %5.1f / %3d          | %3.0f%%".format(cols, rows, 360.0 / cols, 180.0 / rows, avg, max, frac * 100))
            assertTrue(frac < 1.0)
        }
    }
}
