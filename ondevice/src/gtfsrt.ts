// GTFS-Realtime, decoded on the device.
//
// The server unpacks these feeds with the generated protobuf library. A
// phone could carry one too, but the app reads a dozen fields out of a
// large schema, and protobuf's wire format is simple enough to read
// directly: every field is a varint tag saying which number and what
// shape, then a value. Unknown fields are skipped by shape, which is
// exactly how protobuf is designed to be forward-compatible — a feed that
// starts publishing occupancy or carriage data will not trouble this.
//
// Field numbers come from gtfs-realtime.proto and are fixed by the spec:
//
//   FeedMessage.entity            = 2
//   FeedEntity.id                 = 1
//   FeedEntity.trip_update        = 3
//   FeedEntity.vehicle            = 4
//   FeedEntity.alert              = 5
//   TripUpdate.trip               = 1
//   TripUpdate.stop_time_update   = 2
//   TripDescriptor.trip_id        = 1
//   TripDescriptor.start_date     = 3
//   TripDescriptor.schedule_relationship = 4
//   StopTimeUpdate.stop_sequence  = 1
//   StopTimeUpdate.arrival        = 2
//   StopTimeUpdate.departure      = 3
//   StopTimeUpdate.stop_id        = 4
//   StopTimeEvent.delay           = 1
//   StopTimeEvent.time            = 2
//   VehiclePosition.trip          = 1
//   VehiclePosition.position      = 2
//   Position.latitude             = 1  (float)
//   Position.longitude            = 2  (float)

export interface StopTimeUpdate {
  stopSequence: number | null;
  stopId: string | null;
  time: number | null;          // epoch seconds, arrival preferred
  delay: number | null;         // seconds
}

export interface TripUpdate {
  tripId: string;
  startDate: string | null;
  scheduleRelationship: number | null;
  stopTimeUpdates: StopTimeUpdate[];
}

export interface VehiclePosition {
  tripId: string | null;
  lat: number | null;
  lon: number | null;
}

export interface Feed {
  tripUpdates: TripUpdate[];
  vehicles: VehiclePosition[];
}

const WIRE_VARINT = 0, WIRE_64 = 1, WIRE_LEN = 2, WIRE_32 = 5;

class Reader {
  private p = 0;
  private readonly b: Uint8Array;
  private readonly end: number;
  // Fields assigned explicitly rather than as constructor parameter
  // properties: those emit code, so a runtime that only strips types
  // (node --experimental-strip-types, which is how these are tested)
  // refuses them.
  constructor(b: Uint8Array, end?: number) {
    this.b = b;
    this.end = end ?? b.length;
  }
  get done(): boolean { return this.p >= this.end; }

  /**
   * Unsigned varint.
   *
   * Accumulated by multiplication rather than shifting: `<<` in JavaScript
   * is a 32-bit operation, so a shift past 31 wraps around. Everything
   * unsigned in these feeds — epoch times, stop sequences, enums — is far
   * inside 2^53 and exact here.
   */
  varint(): number {
    let v = 0, mul = 1;
    for (;;) {
      const byte = this.b[this.p++]!;
      v += (byte & 0x7f) * mul;
      mul *= 128;
      if (!(byte & 0x80)) break;
      if (mul > 2 ** 70) throw new Error("varint too long");
    }
    return v;
  }

  /**
   * Signed varint.
   *
   * A delay of -36 seconds is not a small number on the wire: protobuf
   * writes negative int32 and int64 as a full 64-bit two's complement, ten
   * bytes of it. Reading that needs all 64 bits, which a double cannot
   * hold, so this one field goes through BigInt — the live SEQ feed is
   * full of them (a service running early), and truncating gave delays of
   * seventy-two quadrillion seconds.
   */
  svarint(): number {
    let v = 0n, shift = 0n;
    for (;;) {
      const byte = this.b[this.p++]!;
      v |= BigInt(byte & 0x7f) << shift;
      shift += 7n;
      if (!(byte & 0x80)) break;
      if (shift > 70n) throw new Error("varint too long");
    }
    return Number(BigInt.asIntN(64, v));
  }

  bytes(): Uint8Array {
    const n = this.varint();
    const out = this.b.subarray(this.p, this.p + n);
    this.p += n;
    return out;
  }

  string(): string {
    return new TextDecoder().decode(this.bytes());
  }

  float(): number {
    const v = new DataView(this.b.buffer, this.b.byteOffset + this.p, 4)
      .getFloat32(0, true);
    this.p += 4;
    return v;
  }

  /** Step over a field whose number we do not care about. */
  skip(wire: number): void {
    switch (wire) {
      case WIRE_VARINT: this.varint(); break;
      case WIRE_64: this.p += 8; break;
      // NOT `this.p += this.varint()`. A compound assignment reads the
      // left side BEFORE evaluating the right, so the bytes varint()
      // consumes reading the length would be thrown away and every
      // skipped field would land short by one to five bytes.
      case WIRE_LEN: { const n = this.varint(); this.p += n; break; }
      case WIRE_32: this.p += 4; break;
      // A decoder that cannot say WHERE it lost the thread is very
      // hard to debug against a megabyte of someone else's bytes.
      default: throw new Error(`unknown wire type ${wire} at byte ${this.p - 1}`);
    }
  }

  /** Walk the fields of a message, handing each to `on`. */
  each(on: (field: number, wire: number, r: Reader) => boolean): void {
    while (!this.done) {
      const tag = this.varint();
      const field = tag >>> 3, wire = tag & 7;
      if (!on(field, wire, this)) this.skip(wire);
    }
  }

  sub(): Reader {
    const b = this.bytes();
    return new Reader(b);
  }
}

function readStopTimeEvent(r: Reader): { time: number | null; delay: number | null } {
  let time: number | null = null, delay: number | null = null;
  r.each((f, w, rr) => {
    if (f === 1 && w === WIRE_VARINT) { delay = rr.svarint(); return true; }
    if (f === 2 && w === WIRE_VARINT) { time = rr.varint(); return true; }
    return false;
  });
  return { time, delay };
}

function readStopTimeUpdate(r: Reader): StopTimeUpdate {
  const out: StopTimeUpdate = { stopSequence: null, stopId: null, time: null, delay: null };
  // Arrival wins over departure, matching the server: a board says when
  // the thing gets here.
  let sawArrival = false;
  r.each((f, w, rr) => {
    if (f === 1 && w === WIRE_VARINT) { out.stopSequence = rr.varint(); return true; }
    if (f === 2 && w === WIRE_LEN) {
      const ev = readStopTimeEvent(rr.sub());
      out.time = ev.time; out.delay = ev.delay; sawArrival = true; return true;
    }
    if (f === 3 && w === WIRE_LEN) {
      const ev = readStopTimeEvent(rr.sub());
      if (!sawArrival) { out.time = ev.time; out.delay = ev.delay; }
      return true;
    }
    if (f === 4 && w === WIRE_LEN) { out.stopId = rr.string(); return true; }
    return false;
  });
  return out;
}

function readTrip(r: Reader): { tripId: string; startDate: string | null;
                                rel: number | null } {
  let tripId = "", startDate: string | null = null, rel: number | null = null;
  r.each((f, w, rr) => {
    if (f === 1 && w === WIRE_LEN) { tripId = rr.string(); return true; }
    if (f === 3 && w === WIRE_LEN) { startDate = rr.string(); return true; }
    if (f === 4 && w === WIRE_VARINT) { rel = rr.varint(); return true; }
    return false;
  });
  return { tripId, startDate, rel };
}

function readTripUpdate(r: Reader): TripUpdate {
  let trip = { tripId: "", startDate: null as string | null, rel: null as number | null };
  const stus: StopTimeUpdate[] = [];
  r.each((f, w, rr) => {
    if (f === 1 && w === WIRE_LEN) { trip = readTrip(rr.sub()); return true; }
    if (f === 2 && w === WIRE_LEN) { stus.push(readStopTimeUpdate(rr.sub())); return true; }
    return false;
  });
  return { tripId: trip.tripId, startDate: trip.startDate,
           scheduleRelationship: trip.rel, stopTimeUpdates: stus };
}

function readVehicle(r: Reader): VehiclePosition {
  let tripId: string | null = null, lat: number | null = null, lon: number | null = null;
  r.each((f, w, rr) => {
    if (f === 1 && w === WIRE_LEN) { tripId = readTrip(rr.sub()).tripId || null; return true; }
    if (f === 2 && w === WIRE_LEN) {
      rr.sub().each((pf, pw, pr) => {
        if (pf === 1 && pw === WIRE_32) { lat = pr.float(); return true; }
        if (pf === 2 && pw === WIRE_32) { lon = pr.float(); return true; }
        return false;
      });
      return true;
    }
    return false;
  });
  return { tripId, lat, lon };
}

/** Decode a GTFS-Realtime FeedMessage. */
export function decodeFeed(bytes: Uint8Array): Feed {
  const feed: Feed = { tripUpdates: [], vehicles: [] };
  new Reader(bytes).each((f, w, r) => {
    if (f !== 2 || w !== WIRE_LEN) return false;         // FeedMessage.entity
    r.sub().each((ef, ew, er) => {
      if (ef === 3 && ew === WIRE_LEN) {
        feed.tripUpdates.push(readTripUpdate(er.sub())); return true;
      }
      if (ef === 4 && ew === WIRE_LEN) {
        feed.vehicles.push(readVehicle(er.sub())); return true;
      }
      return false;
    });
    return true;
  });
  return feed;
}
