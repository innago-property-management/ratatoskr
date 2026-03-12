"""Tests for keyword-ranked schema truncation (Wave 4 Phase 1)."""

from __future__ import annotations

from api_agent.schema.reducer import rank_and_truncate

# ---------------------------------------------------------------------------
# Sample schemas for tests
# ---------------------------------------------------------------------------

GRAPHQL_SCHEMA = """\
<queries>
searchFlights(origin: String!, dest: String!) -> [Flight]  # Search available flights
bookFlight(flightId: ID!) -> Booking  # Book a flight
cancelBooking(bookingId: ID!) -> Boolean  # Cancel a booking
getPassenger(id: ID!) -> Passenger  # Get passenger info
listAirports() -> [Airport]  # List all airports
getWeather(location: String!) -> Weather  # Get weather info
searchHotels(city: String!) -> [Hotel]  # Search hotels
reserveHotel(hotelId: ID!) -> Reservation  # Reserve a hotel
getCarRentals(location: String!) -> [CarRental]  # Get car rentals
insuranceQuote(tripId: ID!) -> InsuranceQuote  # Get insurance quote

<types>
Flight {
  id: ID!
  origin: String!
  destination: String!
  price: Float!
  status: FlightStatus!
}
Booking {
  id: ID!
  flight: Flight!
  passenger: Passenger!
  confirmed: Boolean!
}
Passenger {
  id: ID!
  name: String!
  email: String!
}
Airport {
  code: String!
  name: String!
  city: String!
}
Weather {
  temperature: Float!
  condition: String!
}
Hotel {
  id: ID!
  name: String!
  city: String!
  pricePerNight: Float!
}
Reservation {
  id: ID!
  hotel: Hotel!
  checkIn: String!
  checkOut: String!
}
CarRental {
  id: ID!
  vehicle: String!
  pricePerDay: Float!
}
InsuranceQuote {
  id: ID!
  coverage: String!
  premium: Float!
}

<enums>
FlightStatus: SCHEDULED | DELAYED | CANCELLED

<auth>
bearerAuth: HTTP bearer JWT"""

REST_SCHEMA = """\
<endpoints>
GET /flights({origin, dest}) -> FlightList  # Search flights
POST /bookings(body: BookingRequest!) -> Booking  # Create booking
GET /bookings/{id}() -> Booking  # Get booking by ID
DELETE /bookings/{id}() -> void  # Cancel booking
GET /hotels({city}) -> HotelList  # Search hotels
POST /reservations(body: ReservationRequest!) -> Reservation  # Create reservation

<schemas>
Flight { id: string, origin: string, destination: string, price: number }
BookingRequest { flightId: string!, passengerName: string! }
Hotel { id: string, name: string, city: string, pricePerNight: number }
ReservationRequest { hotelId: string!, checkIn: string!, checkOut: string! }

<auth>
bearerAuth: HTTP bearer JWT"""

GRPC_SCHEMA = """\
service FlightService {
  rpc SearchFlights(SearchRequest) returns (FlightList)
  rpc BookFlight(BookRequest) returns (Booking)
}
service BookingService {
  rpc GetBooking(BookingId) returns (Booking)
  rpc CancelBooking(BookingId) returns (Empty)
}
service HotelService {
  rpc SearchHotels(HotelSearchRequest) returns (HotelList)
  rpc ReserveHotel(ReserveRequest) returns (Reservation)
}
service WeatherService {
  rpc GetWeather(LocationRequest) returns (WeatherResponse)
  rpc GetForecast(ForecastRequest) returns (ForecastResponse)
}"""


class TestUnderThreshold:
    """Test 1: Schema under threshold -> returned as-is, no truncation marker."""

    def test_under_threshold_returns_unchanged(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "find flights", threshold=99999)
        assert result == GRAPHQL_SCHEMA
        assert "[SCHEMA RANKED AND TRUNCATED" not in result


class TestRelevantRankedFirst:
    """Test 2: Relevant endpoints appear first in output."""

    def test_relevant_endpoints_ranked_first(self) -> None:
        # Use a generous threshold so nothing is dropped — just check order
        result = rank_and_truncate(GRAPHQL_SCHEMA, "search for flights", threshold=99999)
        # Flight-related items should appear before hotel/weather/insurance items
        flight_pos = result.find("searchFlights")
        hotel_pos = result.find("searchHotels")
        insurance_pos = result.find("insuranceQuote")
        assert flight_pos < hotel_pos
        assert flight_pos < insurance_pos


class TestIrrelevantDropped:
    """Test 3: Irrelevant endpoints dropped when over budget."""

    def test_irrelevant_endpoints_dropped_when_over_budget(self) -> None:
        # Set threshold tight enough that not everything fits
        result = rank_and_truncate(GRAPHQL_SCHEMA, "booking", threshold=600)
        # Booking-related content should be present
        assert "bookFlight" in result or "cancelBooking" in result or "Booking" in result
        # Completely unrelated content should be dropped
        assert "insuranceQuote" not in result or "getWeather" not in result


class TestSectionHeadersKept:
    """Test 4: Section headers are always included for present blocks."""

    def test_section_headers_always_included(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "booking", threshold=600)
        # If any query-section block survived, <queries> header should be present
        if "bookFlight" in result or "cancelBooking" in result:
            assert "<queries>" in result
        # If any type-section block survived, <types> header should be present
        if "Booking {" in result:
            assert "<types>" in result


class TestAuthAlwaysKept:
    """Test 5: Auth section always survives truncation."""

    def test_auth_section_always_kept(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "flights", threshold=600)
        assert "<auth>" in result
        assert "bearerAuth" in result


class TestTruncationMarker:
    """Test 6: Truncation marker when items are dropped."""

    def test_truncation_marker_appended(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "flights", threshold=400)
        assert "[SCHEMA RANKED AND TRUNCATED" in result
        assert "search_schema()" in result
        # Should mention count of dropped items
        assert "remaining" in result


class TestNoMarkerWhenNothingDropped:
    """Test 7: No truncation marker when everything fits."""

    def test_no_truncation_marker_when_nothing_dropped(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "flights", threshold=99999)
        assert "[SCHEMA RANKED AND TRUNCATED" not in result


class TestMultilineBlocksKeptTogether:
    """Test 8: Multi-line type blocks stay as one unit."""

    def test_multiline_type_blocks_kept_together(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "flight status", threshold=800)
        # If Flight type is included, it should be complete (has closing brace)
        if "Flight {" in result:
            flight_start = result.find("Flight {")
            remaining = result[flight_start:]
            # Find the next closing brace — should contain the full type
            assert "status: FlightStatus!" in remaining or "price: Float!" in remaining


class TestZeroKeywordBlocksDeprioritized:
    """Test 9: Blocks with no keyword match go to the end."""

    def test_zero_keyword_blocks_deprioritized(self) -> None:
        result = rank_and_truncate(GRAPHQL_SCHEMA, "booking", threshold=600)
        # Booking-related blocks should appear before unrelated ones
        if "Booking" in result and "Weather" in result:
            assert result.find("Booking") < result.find("Weather")


class TestEmptyQuestion:
    """Test 10: Empty question -> hard truncate (can't rank)."""

    def test_empty_question_returns_hard_truncation(self) -> None:
        long_schema = GRAPHQL_SCHEMA
        threshold = 200
        result = rank_and_truncate(long_schema, "", threshold=threshold)
        # Should still truncate to roughly the threshold
        # The result should contain a truncation marker
        assert "[SCHEMA" in result or len(result) <= threshold


class TestGrpcServiceBlocks:
    """Test 11: gRPC service blocks scored and ranked correctly."""

    def test_grpc_service_blocks_ranked(self) -> None:
        result = rank_and_truncate(GRPC_SCHEMA, "hotel reservation", threshold=300)
        # HotelService should be prioritized
        assert "HotelService" in result
        # Less relevant services may be dropped
        if "[SCHEMA RANKED AND TRUNCATED" in result:
            # If truncation happened, weather should be less likely to survive
            hotel_present = "HotelService" in result
            assert hotel_present


class TestStableSort:
    """Test 12: Blocks with equal scores maintain original document order."""

    def test_stable_sort_preserves_order_for_ties(self) -> None:
        schema = """\
<endpoints>
GET /alpha() -> Alpha  # First endpoint
GET /beta() -> Beta  # Second endpoint
GET /gamma() -> Gamma  # Third endpoint"""
        # Question with no keywords matching any block — all score 0
        result = rank_and_truncate(schema, "zzzzz_nomatch", threshold=99999)
        # Original order should be preserved
        alpha_pos = result.find("/alpha")
        beta_pos = result.find("/beta")
        gamma_pos = result.find("/gamma")
        assert alpha_pos < beta_pos < gamma_pos
