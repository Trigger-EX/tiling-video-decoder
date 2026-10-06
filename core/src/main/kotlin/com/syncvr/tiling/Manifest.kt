package com.syncvr.tiling

import org.json.JSONObject

/** Row/column of a tile; row 0 is the top of the picture, column 0 its left edge. */
data class TileId(val row: Int, val col: Int) {
    override fun toString() = "t_${row}_$col"
}

data class Grid(val cols: Int, val rows: Int, val tileWidth: Int, val tileHeight: Int, val pad: Int) {
    val width get() = cols * tileWidth
    val height get() = rows * tileHeight
    val tileCount get() = cols * rows
    /** Size of the stored (padded) tile video. */
    val codedWidth get() = tileWidth + 2 * pad
    val codedHeight get() = tileHeight + 2 * pad

    fun all(): List<TileId> = (0 until rows).flatMap { r -> (0 until cols).map { c -> TileId(r, c) } }
}

/** Parsed `manifest.json` written by tools/tiler.py. */
data class Tileset(
    val grid: Grid,
    val fps: Double,
    val durationSeconds: Double,
    val codec: String,
    val gopSeconds: Double,
    val baseFile: String,
    val baseWidth: Int,
    val baseHeight: Int,
    private val tileFiles: Map<TileId, String>,
) {
    fun fileOf(tile: TileId): String = tileFiles[tile] ?: error("no file for $tile")

    companion object {
        const val SUPPORTED_VERSION = 1

        fun parse(json: String): Tileset {
            val o = JSONObject(json)
            val version = o.getInt("version")
            require(version == SUPPORTED_VERSION) { "unsupported tileset version $version" }
            require(o.getString("projection") == "equirect") { "only equirect tilesets are supported" }
            require(o.getString("stereo") == "none") { "stereo tilesets are not supported yet" }
            val g = o.getJSONObject("grid")
            val grid = Grid(g.getInt("cols"), g.getInt("rows"), g.getInt("tileWidth"), g.getInt("tileHeight"), g.getInt("pad"))
            val tiles = o.getJSONArray("tiles")
            val files = HashMap<TileId, String>()
            for (i in 0 until tiles.length()) {
                val t = tiles.getJSONObject(i)
                files[TileId(t.getInt("row"), t.getInt("col"))] = t.getString("file")
            }
            require(files.keys == grid.all().toSet()) { "manifest lists ${files.size} tiles, grid needs ${grid.tileCount}" }
            val base = o.getJSONObject("base")
            return Tileset(
                grid, o.getDouble("fps"), o.getDouble("durationSeconds"), o.getString("codec"),
                o.getDouble("gopSeconds"), base.getString("file"), base.getInt("width"), base.getInt("height"), files,
            )
        }
    }
}
