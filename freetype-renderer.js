(() => {
  const MAX_GLYPH_WIDTH = 24;
  const MAX_FONT_HEIGHT = 127;
  const SHIFT_X = 2;
  const SHIFT_Y = 1;
  let enginePromise;

  function getEngine() {
    if (!enginePromise) {
      enginePromise = (async () => {
        const factory = window.Module;
        if (typeof factory !== 'function') {
          throw new Error('The local FreeType WebAssembly module did not load.');
        }
        const module = await factory();
        module._init_constants();

        const libraryOutput = module._malloc(4);
        const initError = module.ccall(
          'FT_Init_FreeType', 'number', ['number'], [libraryOutput]
        );
        if (initError) {
          module._free(libraryOutput);
          throw new Error(`FreeType initialization failed (${initError}).`);
        }
        const library = module.getValue(libraryOutput, 'i32');
        module._free(libraryOutput);
        return {
          module,
          library,
          newMemoryFace: module.cwrap(
            'FT_New_Memory_Face', 'number',
            ['number', 'number', 'number', 'number', 'number']
          ),
          setCharSize: module.cwrap(
            'FT_Set_Char_Size', 'number',
            ['number', 'number', 'number', 'number', 'number']
          ),
          setPixelSizes: module.cwrap(
            'FT_Set_Pixel_Sizes', 'number', ['number', 'number', 'number']
          ),
          getCharIndex: module.cwrap(
            'FT_Get_Char_Index', 'number', ['number', 'number']
          ),
          loadGlyph: module.cwrap(
            'FT_Load_Glyph', 'number', ['number', 'number', 'number']
          ),
          renderGlyph: module.cwrap(
            'FT_Render_Glyph', 'number', ['number', 'number']
          ),
          doneFace: module.cwrap('FT_Done_Face', 'number', ['number'])
        };
      })();
      enginePromise.catch(() => { enginePromise = null; });
    }
    return enginePromise;
  }

  function createFace(engine, fontBytes, size) {
    const { module } = engine;
    const fontPointer = module._malloc(fontBytes.length);
    if (!fontPointer) throw new Error('FreeType could not allocate the font data.');
    module.writeArrayToMemory(fontBytes, fontPointer);
    const faceOutput = module._malloc(4);
    if (!faceOutput) {
      module._free(fontPointer);
      throw new Error('FreeType could not allocate a font face.');
    }

    let facePointer = 0;
    try {
      const createError = engine.newMemoryFace(
        engine.library, fontPointer, fontBytes.length, 0, faceOutput
      );
      if (createError) throw new Error(`FreeType could not open this font (${createError}).`);
      facePointer = module.getValue(faceOutput, 'i32');
      const charSizeError = engine.setCharSize(facePointer, 0, size * 64, 300, 300);
      if (charSizeError) throw new Error(`FreeType could not set the font size (${charSizeError}).`);
      const pixelSizeError = engine.setPixelSizes(facePointer, 0, size);
      if (pixelSizeError) throw new Error(`FreeType could not set the pixel size (${pixelSizeError}).`);
      return { pointer: facePointer, fontPointer };
    } catch (error) {
      if (facePointer) engine.doneFace(facePointer);
      module._free(fontPointer);
      throw error;
    } finally {
      module._free(faceOutput);
    }
  }

  function destroyFace(engine, face) {
    const error = engine.doneFace(face.pointer);
    engine.module._free(face.fontPointer);
    if (error) throw new Error(`FreeType could not release the font face (${error}).`);
  }

  function bitmapFromSlot(engine, slotPointer) {
    const { module } = engine;
    const offset = (name, type = 'i32') =>
      module.getValue(slotPointer + module[`OFFSET_GLYPH_${name}`], type);
    const width = offset('BITMAP_WIDTH');
    const height = offset('BITMAP_ROWS');
    const pitch = offset('BITMAP_PITCH');
    const pixelMode = offset('BITMAP_PIXEL_MODE', 'i8');
    const grayLevels = offset('BITMAP_NUM_GRAYS', 'i16');
    const buffer = module.getValue(
      slotPointer + module.OFFSET_GLYPH_BITMAP_BUFFER, 'i32'
    );
    const absolutePitch = Math.abs(pitch);
    const rows = [];

    for (let y = 0; y < height; y++) {
      const sourceY = pitch < 0 ? height - 1 - y : y;
      const rowPointer = buffer + sourceY * absolutePitch;
      const row = new Float32Array(width);
      for (let x = 0; x < width; x++) {
        if (pixelMode === module.FT_PIXEL_MODE_MONO) {
          const value = module.getValue(rowPointer + (x >> 3), 'i8') & 0xff;
          row[x] = value & (0x80 >> (x & 7)) ? 1 : 0;
        } else {
          const value = module.getValue(rowPointer + x, 'i8') & 0xff;
          row[x] = value / Math.max(1, grayLevels - 1);
        }
      }
      rows.push(row);
    }
    return {
      width,
      height,
      left: offset('BITMAP_LEFT'),
      top: offset('BITMAP_TOP'),
      rows,
      linearAdvance: offset('LINEAR_HORI_ADVANCE') / 65536
    };
  }

  function loadBitmap(engine, face, codepoint, flags, renderMode) {
    const { module } = engine;
    const index = engine.getCharIndex(face.pointer, codepoint);
    if (!index) return null;
    const error = engine.loadGlyph(face.pointer, index, flags);
    if (error) throw new Error(`FreeType could not render U+${codepoint.toString(16).toUpperCase()} (${error}).`);
    const slot = module.getValue(
      face.pointer + module.OFFSET_FACE_GLYPH, 'i32'
    );
    const advance = module.getValue(
      slot + module.OFFSET_GLYPH_LINEAR_HORI_ADVANCE, 'i32'
    ) / 65536;
    const renderError = engine.renderGlyph(slot, renderMode);
    if (renderError) throw new Error(`FreeType bitmap rendering failed (${renderError}).`);
    return { ...bitmapFromSlot(engine, slot), linearAdvance: advance };
  }

  function makeCoverage(bitmap, canvasWidth, canvasHeight, baseY, originX) {
    const coverage = Array.from(
      { length: canvasHeight }, () => new Float32Array(canvasWidth)
    );
    for (let y = 0; y < bitmap.height; y++) {
      const targetY = baseY - bitmap.top + y;
      if (targetY < 0 || targetY >= canvasHeight) continue;
      for (let x = 0; x < bitmap.width; x++) {
        const targetX = originX + bitmap.left + x;
        if (targetX >= 0 && targetX < canvasWidth) {
          coverage[targetY][targetX] = bitmap.rows[y][x];
        }
      }
    }
    return coverage;
  }

  function scoreGlyphs(font, outlines, size) {
    const padding = Math.max(2, size);
    let intersectionTotal = 0;
    let unionTotal = 0;
    let widthPenalty = 0;
    let widthMatches = 0;
    let compared = 0;

    for (const [key, glyph] of Object.entries(font.glyphs)) {
      const outline = outlines[key];
      if (!outline) continue;
      const canvasWidth = glyph.width + 2 * padding + 2 * SHIFT_X + 2;
      const canvasHeight = font.height + 2 * padding;
      const coverage = makeCoverage(
        outline,
        canvasWidth,
        canvasHeight,
        padding + font.baseline,
        padding + SHIFT_X + 1
      );
      let bestIntersection = 0;
      let bestUnion = 0;
      let bestRatio = -1;

      for (let dy = -SHIFT_Y; dy <= SHIFT_Y; dy++) {
        for (let dx = -SHIFT_X; dx <= SHIFT_X; dx++) {
          let intersection = 0;
          let union = 0;
          for (let y = 0; y < canvasHeight; y++) {
            const bitmapY = y - padding - dy;
            const bitmapRow = bitmapY >= 0 && bitmapY < font.height
              ? glyph.rows[bitmapY]
              : null;
            const coverageRow = coverage[y];
            for (let x = 0; x < canvasWidth; x++) {
              const bitmapX = x - padding - SHIFT_X - 1 - dx;
              const bitmapInk = bitmapRow && bitmapX >= 0 && bitmapX < glyph.width
                && bitmapRow[bitmapX] === '#';
              const outlineInk = coverageRow[x];
              if (!bitmapInk && !outlineInk) continue;
              if (bitmapInk) {
                intersection += outlineInk;
                union += Math.max(1, outlineInk);
              } else {
                union += outlineInk;
              }
            }
          }
          const ratio = union ? intersection / union : 0;
          if (ratio > bestRatio) {
            bestRatio = ratio;
            bestIntersection = intersection;
            bestUnion = union;
          }
        }
      }

      if (!bestUnion) continue;
      intersectionTotal += bestIntersection;
      unionTotal += bestUnion;
      compared++;
      const widthDifference = Math.abs(glyph.width - outline.linearAdvance);
      const penalty = Math.max(0, widthDifference - 0.5) * font.height;
      widthPenalty += penalty;
      if (penalty === 0) widthMatches++;
    }

    return {
      score: unionTotal
        ? 100 * intersectionTotal / (unionTotal + widthPenalty)
        : 0,
      ink: unionTotal ? 100 * intersectionTotal / unionTotal : 0,
      width_match: compared ? 100 * widthMatches / compared : 0,
      compared,
      requested: 95
    };
  }

  function firstInkRow(rows) {
    const index = rows.findIndex(row => row.includes('#'));
    return index < 0 ? 0 : index;
  }

  function renderFont(engine, fontBytes, size, settings, warnings) {
    const { module } = engine;
    const face = createFace(engine, fontBytes, size);
    const glyphs = {};
    const outlines = {};
    const monoFlags = module.FT_LOAD_TARGET_MONO | module.FT_LOAD_FORCE_AUTOHINT;
    const rawFlags = module.FT_LOAD_NO_HINTING;
    let top = Infinity;
    let bottom = -Infinity;

    try {
      for (let codepoint = 32; codepoint <= 126; codepoint++) {
        const mono = loadBitmap(
          engine, face, codepoint, monoFlags, module.FT_RENDER_MODE_MONO
        );
        if (!mono) {
          warnings.push(`Font has no glyph for U+${codepoint.toString(16).toUpperCase().padStart(4, '0')}.`);
          continue;
        }
        const advance = Math.max(1, Math.ceil(mono.linearAdvance - 0.001));
        const left = Math.max(-settings.overhang, Math.min(0, mono.left));
        const right = Math.min(
          settings.overhang,
          Math.max(0, mono.left + mono.width - advance)
        );
        const start = left;
        const end = advance + right;
        let width = Math.max(1, end - start);
        let rows = Array.from({ length: mono.height }, (_, y) => {
          const row = Array(width).fill(' ');
          for (let x = 0; x < mono.width; x++) {
            const targetX = mono.left - start + x;
            if (targetX >= 0 && targetX < width && mono.rows[y][x] >= 0.5) {
              row[targetX] = '#';
            }
          }
          return row;
        });

        if (settings.tracking > 0) {
          rows = rows.map(row => row.concat(Array(settings.tracking).fill(' ')));
          width += settings.tracking;
        } else if (settings.tracking < 0) {
          width = Math.max(1, width + settings.tracking);
          rows = rows.map(row => row.slice(0, width));
        }
        if (width > MAX_GLYPH_WIDTH) {
          throw new Error(
            `Character ${JSON.stringify(String.fromCharCode(codepoint))} is ${width}px wide; ` +
            `FontLibC supports at most ${MAX_GLYPH_WIDTH}px.`
          );
        }
        glyphs[codepoint] = { width, rows, bitmapTop: mono.top };
        for (let y = 0; y < mono.height; y++) {
          if (!rows[y].includes('#')) continue;
          const rowTop = y - mono.top;
          top = Math.min(top, rowTop);
          bottom = Math.max(bottom, rowTop);
        }

        const outline = loadBitmap(
          engine, face, codepoint, rawFlags, module.FT_RENDER_MODE_NORMAL
        );
        if (outline) outlines[codepoint] = outline;
      }
    } finally {
      destroyFace(engine, face);
    }

    if (!Number.isFinite(top) || !Number.isFinite(bottom)) {
      throw new Error('This font has no visible printable ASCII glyphs.');
    }
    const height = bottom - top + 1;
    if (height > MAX_FONT_HEIGHT) {
      throw new Error(`Font height is ${height}px; FontLibC supports at most ${MAX_FONT_HEIGHT}px.`);
    }

    for (const glyph of Object.values(glyphs)) {
      const rawRows = glyph.rows;
      glyph.rows = Array.from({ length: height }, (_, y) => {
        const sourceY = y + top + glyph.bitmapTop;
        return rawRows[sourceY]?.join('') || ' '.repeat(glyph.width);
      });
      delete glyph.bitmapTop;
    }

    const record = {
      size,
      height,
      baseline: -top,
      glyphs
    };
    record.cap_height = firstInkRow(glyphs[72]?.rows || []);
    record.x_height = firstInkRow(glyphs[120]?.rows || []);
    return {
      record,
      match: scoreGlyphs(record, outlines, size)
    };
  }

  function put16(bytes, offset, value) {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = (value >>> 8) & 0xff;
  }

  function put24(bytes, offset, value) {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = (value >>> 8) & 0xff;
    bytes[offset + 2] = (value >>> 16) & 0xff;
  }

  function serializeFont(record, weight) {
    const codepoints = Object.keys(record.glyphs).map(Number).sort((a, b) => a - b);
    const first = codepoints[0];
    const last = codepoints[codepoints.length - 1];
    const count = last - first + 1;
    const widthsOffset = 18;
    const offsetsOffset = widthsOffset + count;
    const bitmapOffset = offsetsOffset + count * 2;
    const glyphData = [];
    let bitmapLength = 0;

    for (let codepoint = first; codepoint <= last; codepoint++) {
      const glyph = record.glyphs[codepoint] || {
        width: 1,
        rows: Array(record.height).fill(' '.repeat(1))
      };
      if (glyph.width < 1 || glyph.width > MAX_GLYPH_WIDTH
          || glyph.rows.length !== record.height
          || glyph.rows.some(row => row.length !== glyph.width)) {
        throw new Error(`Invalid bitmap dimensions for U+${codepoint.toString(16).toUpperCase()}.`);
      }
      const columns = Math.ceil(glyph.width / 8);
      const bitmap = new Uint8Array(record.height * columns);
      let bitmapAt = 0;
      for (const row of glyph.rows) {
        for (let column = columns - 1; column >= 0; column--) {
          let value = 0;
          for (let bit = 0; bit < 8; bit++) {
            if (row[column * 8 + bit] === '#') value |= 0x80 >> bit;
          }
          bitmap[bitmapAt++] = value;
        }
      }
      glyphData.push({ glyph, columns, bitmap });
      bitmapLength += bitmap.length;
    }

    const bytes = new Uint8Array(bitmapOffset + bitmapLength);
    bytes[1] = record.height;
    bytes[2] = count & 0xff;
    bytes[3] = first;
    put24(bytes, 4, widthsOffset);
    put24(bytes, 7, offsetsOffset);
    bytes[13] = weight === 'bold' ? 0xc0 : 0x80;
    bytes[15] = record.cap_height;
    bytes[16] = record.x_height;
    bytes[17] = record.baseline;

    let currentBitmap = bitmapOffset;
    glyphData.forEach(({ glyph, columns, bitmap }, index) => {
      bytes[widthsOffset + index] = glyph.width;
      put16(bytes, offsetsOffset + index * 2, currentBitmap - 2 + columns - 1);
      bytes.set(bitmap, currentBitmap);
      currentBitmap += bitmap.length;
    });
    return bytes;
  }

  function serializeFontPack(records, name, weight) {
    const fonts = records.map(record => serializeFont(record, weight));
    const metadata = [name, null, null, null, '1', 'ASCII'];
    const metadataSize = 21;
    let location = 12 + fonts.length * 3;
    const metadataOffset = location;
    location += metadataSize;
    const stringOffsets = metadata.map(value => {
      if (value === null) return 0;
      const offset = location;
      location += new TextEncoder().encode(value).length + 1;
      return offset;
    });
    const fontOffsets = fonts.map(font => {
      const offset = location;
      location += font.length;
      return offset;
    });
    if (location > 0xffe8) throw new Error('Font pack is too large for a TI-84 Plus CE AppVar.');

    const result = new Uint8Array(location);
    result.set(new TextEncoder().encode('FONTPACK'));
    put24(result, 8, metadataOffset);
    result[11] = fonts.length;
    fontOffsets.forEach((offset, index) => put24(result, 12 + index * 3, offset));
    put16(result, metadataOffset, metadataSize);
    stringOffsets.forEach((offset, index) => put24(result, metadataOffset + 2 + index * 3, offset));
    metadata.forEach((value, index) => {
      if (value === null) return;
      const encoded = new TextEncoder().encode(value);
      result.set(encoded, stringOffsets[index]);
      result[stringOffsets[index] + encoded.length] = 0;
    });
    fonts.forEach((font, index) => result.set(font, fontOffsets[index]));
    return result;
  }

  function makeAppVar(name, data) {
    if (!data.length || data.length > 0xffe8) {
      throw new Error('Font pack is too large for a TI-84 Plus CE AppVar.');
    }
    const varLength = data.length + 2;
    const entryLength = varLength + 17;
    const result = new Uint8Array(55 + entryLength + 2);
    result.set(new TextEncoder().encode('**TI83F*\x1a\x0a\x00'));
    result.set(new TextEncoder().encode('Created by CEfont Web'), 11);
    put16(result, 53, entryLength);
    const entry = result.subarray(55, 55 + entryLength);
    put16(entry, 0, 13);
    put16(entry, 2, varLength);
    entry[4] = 0x15;
    entry.set(new TextEncoder().encode(name), 5);
    entry[13] = 0;
    entry[14] = 0x80;
    put16(entry, 15, varLength);
    put16(entry, 17, data.length);
    entry.set(data, 19);
    let checksum = 0;
    for (const value of entry) checksum = (checksum + value) & 0xffff;
    put16(result, 55 + entryLength, checksum);
    return result;
  }

  async function convert(fontBuffer, options, onProgress = () => {}) {
    const engine = await getEngine();
    const fontBytes = new Uint8Array(fontBuffer);
    const warnings = [];
    const records = [];
    const matches = [];
    for (let index = 0; index < options.sizes.length; index++) {
      onProgress(Math.round(index / options.sizes.length * 65));
      const rendered = renderFont(
        engine, fontBytes, options.sizes[index], options, warnings
      );
      records.push(rendered.record);
      matches.push({ size: options.sizes[index], ...rendered.match });
    }
    onProgress(75);
    const fontpack = serializeFontPack(records, options.name, options.weight);
    const appvar = makeAppVar(options.name, fontpack);
    onProgress(100);
    return {
      filename: `${options.name}.8xv`,
      bytes: appvar,
      fontRecords: records,
      info: {
        appvar: options.name,
        fonts: records.length,
        sizes: options.sizes,
        warnings,
        matches,
        engine: 'freetype-wasm'
      }
    };
  }

  window.CEfontRenderer = { convert };
})();
