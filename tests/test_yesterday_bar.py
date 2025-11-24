from topstep.client import TopstepClient

client = TopstepClient()

#Test for Expiration of Token
# client._token_exp = 0
# client._token = 'A'

yesterday_bar = client.get_yesterdays_daily_bar_for_symbol("MGC", live=False)

print("Yesterday's daily bar for MGC:")
print(yesterday_bar)
