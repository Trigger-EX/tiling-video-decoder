package com.syncvr.tiling

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.math.tan

/**
 * Same equirect convention as the player's sphere renderer: azimuth 0 looks along -Z, +X is to the
 * right, u = 0.5 + azimuth / 2pi. Rows count from the TOP of the picture (v_img = 0.5 - elevation / pi);
 * GL texture space usually has v up, so flip v when building the draw mesh.
 */
object Sphere {
    fun azimuth(x: Double, z: Double) = atan2(x, -z)
    fun elevation(y: Double) = asin(y.coerceIn(-1.0, 1.0))
    fun dir(az: Double, el: Double) = doubleArrayOf(cos(el) * sin(az), sin(el), -cos(el) * cos(az))
}

/** Head orientation as a unit quaternion (VrApi/Android order: x, y, z, w); rotates view space to world space. */
data class Orientation(val x: Double, val y: Double, val z: Double, val w: Double) {
    fun rotate(v: DoubleArray): DoubleArray {
        // v' = v + 2w(q x v) + 2 q x (q x v)
        val (qx, qy, qz) = Triple(x, y, z)
        val tx = 2 * (qy * v[2] - qz * v[1])
        val ty = 2 * (qz * v[0] - qx * v[2])
        val tz = 2 * (qx * v[1] - qy * v[0])
        return doubleArrayOf(
            v[0] + w * tx + (qy * tz - qz * ty),
            v[1] + w * ty + (qz * tx - qx * tz),
            v[2] + w * tz + (qx * ty - qy * tx),
        )
    }

    companion object {
        val IDENTITY = Orientation(0.0, 0.0, 0.0, 1.0)

        /** Yaw about +Y (positive turns left, as in VrApi), then pitch about X (positive looks up). */
        fun fromYawPitch(yaw: Double, pitch: Double): Orientation {
            val (sy, cy) = sin(yaw / 2) to cos(yaw / 2)
            val (sp, cp) = sin(pitch / 2) to cos(pitch / 2)
            // q = qyaw * qpitch
            return Orientation(cy * sp, sy * cp, -sy * sp, cy * cp)
        }
    }
}

/** Half-angles of the field of view in radians. */
data class Fov(val halfHorizontal: Double, val halfVertical: Double) {
    fun expanded(margin: Double) = Fov(min(halfHorizontal + margin, PI * 0.49), min(halfVertical + margin, PI * 0.49))

    companion object {
        fun degrees(horizontal: Double, vertical: Double) = Fov(Math.toRadians(horizontal / 2), Math.toRadians(vertical / 2))
    }
}

class TileGeometry(val grid: Grid) {
    /** Tile containing the world direction, wrapping horizontally and clamping at the poles. */
    fun tileAt(dir: DoubleArray): TileId {
        val u = 0.5 + Sphere.azimuth(dir[0], dir[2]) / (2 * PI)
        val v = 0.5 - Sphere.elevation(dir[1]) / PI
        val col = floor(u * grid.cols).toInt().mod(grid.cols)
        val row = floor(v * grid.rows).toInt().coerceIn(0, grid.rows - 1)
        return TileId(row, col)
    }

    /** Azimuth range [min, max] in radians of the tile's picture area (excluding padding). */
    fun azimuthRange(tile: TileId): Pair<Double, Double> =
        (-PI + 2 * PI * tile.col / grid.cols) to (-PI + 2 * PI * (tile.col + 1) / grid.cols)

    /** Elevation range [bottom, top] in radians. */
    fun elevationRange(tile: TileId): Pair<Double, Double> =
        (PI / 2 - PI * (tile.row + 1) / grid.rows) to (PI / 2 - PI * tile.row / grid.rows)

    fun centerDir(tile: TileId): DoubleArray {
        val (a0, a1) = azimuthRange(tile)
        val (e0, e1) = elevationRange(tile)
        return Sphere.dir((a0 + a1) / 2, (e0 + e1) / 2)
    }

    /**
     * Texture-space rectangle of the tile video that holds the tile's picture area, excluding the
     * padding: (u0, v0, u1, v1) with v measured from the top of the coded tile. Sampling the inner
     * rectangle only (and drawing the tile's own azimuth/elevation patch) hides tile borders.
     */
    fun innerRect(): FloatArray {
        val cw = grid.codedWidth.toFloat()
        val ch = grid.codedHeight.toFloat()
        val p = grid.pad.toFloat()
        return floatArrayOf(p / cw, p / ch, (p + grid.tileWidth) / cw, (p + grid.tileHeight) / ch)
    }

    /** Angle in radians between two unit vectors. */
    fun angle(a: DoubleArray, b: DoubleArray): Double =
        acos((a[0] * b[0] + a[1] * b[1] + a[2] * b[2]).coerceIn(-1.0, 1.0))
}

/** A tile the view needs, with its distance from the view centre (smaller = more important). */
data class WantedTile(val tile: TileId, val angleRad: Double)

/**
 * Works out which tiles intersect the view frustum by casting a grid of rays across it and
 * collecting the tiles they land in. Ray spacing must be well below a tile's angular size; the
 * default 25x25 rays are about 4 degrees apart across a 100 degree field.
 */
class VisibilityPlanner(private val geometry: TileGeometry, private val raysPerAxis: Int = 25) {
    init {
        require(raysPerAxis >= 2)
    }

    /**
     * Tiles seen by [views] (current head pose first, then predicted poses) with [fov] widened by
     * [margin]. Ordered by angle between the first view's forward axis and the tile centre, so the
     * most central tiles come first; tiles only reached by later views sort after those.
     */
    fun plan(views: List<Orientation>, fov: Fov, margin: Double = 0.0): List<WantedTile> {
        require(views.isNotEmpty())
        val widened = fov.expanded(margin)
        val tx = tan(widened.halfHorizontal)
        val ty = tan(widened.halfVertical)
        val forward = views[0].rotate(doubleArrayOf(0.0, 0.0, -1.0))
        val hit = LinkedHashMap<TileId, Double>()
        for ((index, view) in views.withIndex()) {
            val found = HashSet<TileId>()
            for (i in 0 until raysPerAxis) {
                val sx = -1.0 + 2.0 * i / (raysPerAxis - 1)
                for (j in 0 until raysPerAxis) {
                    val sy = -1.0 + 2.0 * j / (raysPerAxis - 1)
                    val d = view.rotate(normalize(doubleArrayOf(tx * sx, ty * sy, -1.0)))
                    found.add(geometry.tileAt(d))
                }
            }
            for (t in found) {
                val a = geometry.angle(forward, geometry.centerDir(t))
                // Tiles reached only by predicted poses rank after every tile the current pose sees.
                val rank = if (index == 0) a else a + PI * index
                hit[t] = min(hit[t] ?: Double.MAX_VALUE, rank)
            }
        }
        return hit.map { WantedTile(it.key, it.value) }.sortedBy { it.angleRad }
    }

    private fun normalize(v: DoubleArray): DoubleArray {
        val n = sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        return doubleArrayOf(v[0] / n, v[1] / n, v[2] / n)
    }
}

/** Decode cost of a tile selection relative to decoding the full frame. */
object DecodeCost {
    /** Pixels decoded per frame: `tiles` padded tiles plus the base layer. */
    fun pixelsPerFrame(grid: Grid, tiles: Int, baseWidth: Int, baseHeight: Int): Long =
        tiles.toLong() * grid.codedWidth * grid.codedHeight + baseWidth.toLong() * baseHeight

    fun fractionOfFullFrame(grid: Grid, tiles: Int, baseWidth: Int, baseHeight: Int): Double =
        pixelsPerFrame(grid, tiles, baseWidth, baseHeight).toDouble() / (grid.width.toLong() * grid.height)
}

