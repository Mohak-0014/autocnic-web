"""Example business logic. Plain functions: same inputs -> same output."""


def ticket_price(base_fare, seats):
    return round(base_fare * seats, 2)


def booking_fee(member, card_fee_rate, ticket_price):
    # Members pay no fee, so card_fee_rate and ticket_price only matter for
    # non-members -- AutoCNIC discovers this conditional dependency by itself.
    if member:
        return 0.0
    return round(ticket_price * card_fee_rate, 2)


def total_due(ticket_price, booking_fee):
    return round(ticket_price + booking_fee, 2)
